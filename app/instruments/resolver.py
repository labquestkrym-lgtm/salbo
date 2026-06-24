"""Instrument Resolver (R13).

Maps an underlying to its hedging future and its option series, and validates
the relationships: same underlying code, same currency, compatible expiries and
trading session. It does **not** assume equal lot sizes or multipliers between
options and futures — those differences are preserved and consumed by the hedge
sizing logic (R7).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.core.enums import AssetClass, OptionType
from app.core.exceptions import InstrumentResolutionError
from app.models import Instrument


@dataclass(frozen=True, slots=True)
class ResolvedStraddle:
    underlying: Instrument
    future: Instrument
    call: Instrument
    put: Instrument
    strike: Decimal
    expiry: date


@dataclass(frozen=True, slots=True)
class ResolvedPair:
    """A cointegrated pair traded via the front futures of both legs. The spread
    is ``ln(leg_a) - beta*ln(leg_b)``; both legs are futures (freely shortable)."""

    leg_a: Instrument
    leg_b: Instrument
    beta: float


class InstrumentResolver:
    def __init__(self, instruments: list[Instrument]) -> None:
        self._by_symbol: dict[str, Instrument] = {i.symbol: i for i in instruments}
        self._underlyings: dict[str, Instrument] = {}
        self._futures: dict[str, list[Instrument]] = {}
        self._options: dict[str, list[Instrument]] = {}
        for inst in instruments:
            if inst.asset_class in (AssetClass.EQUITY, AssetClass.INDEX):
                self._underlyings[inst.underlying_symbol] = inst
            elif inst.asset_class is AssetClass.FUTURE:
                self._futures.setdefault(inst.underlying_symbol, []).append(inst)
            elif inst.asset_class is AssetClass.OPTION:
                self._options.setdefault(inst.underlying_symbol, []).append(inst)
        for futs in self._futures.values():
            futs.sort(key=lambda f: f.expiry or date.max)

    def get(self, symbol: str) -> Instrument:
        inst = self._by_symbol.get(symbol)
        if inst is None:
            raise InstrumentResolutionError(f"unknown instrument: {symbol}")
        return inst

    def underlying(self, underlying_symbol: str) -> Instrument:
        inst = self._underlyings.get(underlying_symbol)
        if inst is None:
            raise InstrumentResolutionError(f"no underlying for {underlying_symbol}")
        return inst

    def futures_for(self, underlying_symbol: str) -> list[Instrument]:
        return list(self._futures.get(underlying_symbol, []))

    def front_future(self, underlying_symbol: str) -> Instrument:
        """The nearest-expiry future for an underlying (the liquid front month)."""
        futs = self.futures_for(underlying_symbol)
        if not futs:
            raise InstrumentResolutionError(f"no future for {underlying_symbol}")
        return futs[0]  # futures lists are sorted by expiry in __init__

    def resolve_pair(
        self, symbol_a: str, symbol_b: str, *, beta: float = 1.0, on_or_after: date | None = None
    ) -> ResolvedPair:
        """Resolve a tradeable cointegrated pair to the front futures of both legs.
        With ``on_or_after`` the nearest future expiring on/after that date is used
        (skip a front contract near expiry to roll into the next one). Both legs
        must share a currency (a mismatch signals a bad pairing)."""
        if on_or_after is None:
            leg_a = self.front_future(symbol_a)
            leg_b = self.front_future(symbol_b)
        else:
            leg_a = self.nearest_future(symbol_a, on_or_after=on_or_after)
            leg_b = self.nearest_future(symbol_b, on_or_after=on_or_after)
        if leg_a.spec.currency != leg_b.spec.currency:
            raise InstrumentResolutionError(
                f"currency mismatch in pair: {leg_a.spec.currency} vs {leg_b.spec.currency}"
            )
        return ResolvedPair(leg_a=leg_a, leg_b=leg_b, beta=beta)

    def nearest_future(self, underlying_symbol: str, *, on_or_after: date) -> Instrument:
        """The nearest future expiring on/after ``on_or_after`` (i.e. alive
        through the option's expiry)."""
        candidates = [
            f
            for f in self.futures_for(underlying_symbol)
            if f.expiry is not None and f.expiry >= on_or_after
        ]
        if not candidates:
            raise InstrumentResolutionError(
                f"no future for {underlying_symbol} expiring on/after {on_or_after}"
            )
        return candidates[0]

    def expiries(self, underlying_symbol: str) -> list[date]:
        seen = {o.expiry for o in self._options.get(underlying_symbol, []) if o.expiry}
        return sorted(seen)

    def strikes(self, underlying_symbol: str, expiry: date) -> list[Decimal]:
        seen = {
            o.strike
            for o in self._options.get(underlying_symbol, [])
            if o.expiry == expiry and o.strike is not None
        }
        return sorted(seen)

    def find_option(
        self, underlying_symbol: str, expiry: date, strike: Decimal, option_type: OptionType
    ) -> Instrument | None:
        for o in self._options.get(underlying_symbol, []):
            if o.expiry == expiry and o.strike == strike and o.option_type is option_type:
                return o
        return None

    def resolve_straddle(
        self, underlying_symbol: str, *, expiry: date, strike: Decimal
    ) -> ResolvedStraddle:
        underlying = self.underlying(underlying_symbol)
        call = self.find_option(underlying_symbol, expiry, strike, OptionType.CALL)
        put = self.find_option(underlying_symbol, expiry, strike, OptionType.PUT)
        if call is None or put is None:
            raise InstrumentResolutionError(
                f"missing {'call' if call is None else 'put'} at "
                f"{underlying_symbol} {expiry} strike {strike}"
            )
        future = self.nearest_future(underlying_symbol, on_or_after=expiry)
        self.validate_hedge_pair(call, future)
        self.validate_hedge_pair(put, future)
        return ResolvedStraddle(
            underlying=underlying,
            future=future,
            call=call,
            put=put,
            strike=strike,
            expiry=expiry,
        )

    def resolve_straddle_on_future(
        self, underlying_symbol: str, *, expiry: date, strike: Decimal
    ) -> ResolvedStraddle:
        """Options-on-futures straddle: the hedging future is also the spot/forward
        reference (``underlying``). Used for FORTS-style markets (e.g. MOEX via
        T-Invest) where options are written on a future, not on an equity."""
        future = self.nearest_future(underlying_symbol, on_or_after=expiry)
        call = self.find_option(underlying_symbol, expiry, strike, OptionType.CALL)
        put = self.find_option(underlying_symbol, expiry, strike, OptionType.PUT)
        if call is None or put is None:
            raise InstrumentResolutionError(
                f"missing {'call' if call is None else 'put'} at "
                f"{underlying_symbol} {expiry} strike {strike}"
            )
        self.validate_hedge_pair(call, future)
        self.validate_hedge_pair(put, future)
        return ResolvedStraddle(
            underlying=future,  # the future is the spot/forward reference and the hedge
            future=future,
            call=call,
            put=put,
            strike=strike,
            expiry=expiry,
        )

    def validate_hedge_pair(self, option: Instrument, future: Instrument) -> None:
        """Validate that ``future`` can hedge ``option``. Multipliers/lots may
        legitimately differ and are NOT required to match — only the underlying,
        currency and expiry coverage are validated here."""
        if option.underlying_symbol != future.underlying_symbol:
            raise InstrumentResolutionError(
                f"underlying mismatch: option {option.underlying_symbol} vs "
                f"future {future.underlying_symbol}"
            )
        if option.spec.currency != future.spec.currency:
            raise InstrumentResolutionError(
                f"currency mismatch: option {option.spec.currency} vs future {future.spec.currency}"
            )
        if (
            option.expiry is not None
            and future.expiry is not None
            and future.expiry < option.expiry
        ):
            raise InstrumentResolutionError(
                f"future expiry {future.expiry} precedes option expiry {option.expiry}"
            )
