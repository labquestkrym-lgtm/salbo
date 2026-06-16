# Incident Response

> Priority order: **protect capital → preserve information → restore service.**
> When in doubt, stop opening new risk and reconcile; do not panic-close into
> illiquid markets.

## Kill switch behavior
When tripped, the system: (1) blocks new positions, (2) cancels active orders,
(3) re-discovers actual positions via the broker, (4) applies the configured
policy, (5) writes a detailed audit record, (6) sends notifications.

Policies (`KillSwitchPolicy`):
- `HOLD` *(default)* — keep positions, stop acting, alert.
- `HEDGE_ONLY` — keep options, manage only the futures delta hedge.
- `FLATTEN_FUTURES` — close the futures hedge, keep options.
- `CLOSE_ALL` — close everything (only with a confirmed policy).

## Playbooks

### Market-data loss / staleness
Kill switch trips (`MARKET_DATA_LOSS`). Do not trade on stale quotes. Wait for a
fresh, two-sided feed; verify sequence gaps cleared (`FeedHealth`) before resume.

### Broker disconnect
`BROKER_DISCONNECT`. Reconnect, then reconcile orders (`OrderManager.recover`)
and positions before any new action. Never assume fills during the outage.

### Position desync
`POSITION_DESYNC`. Halt new trading. Identify the cause (missed fill, manual
trade, broker correction). Sync local state to the broker (source of truth).
Resume only after a clean reconciliation.

### Unknown order status / timeout
Order goes to `UNKNOWN`. Query the broker authoritatively; never assume filled or
cancelled. Resolve to the broker's state before continuing.

### Daily-loss / margin breach
Critical limit trips the kill switch. Apply policy. Investigate attribution
before re-enabling. Do not raise limits to keep trading through a loss.

### Abnormal price move / gap / wide spread
Pause entries; let the adaptive hedge band and cost gate govern hedging. Avoid
chasing fills across a blown-out spread.

### One leg of a straddle stuck
`StraddleExecutor` reports `ONE_LEG_STUCK` when it filled one leg and could not
unwind. Manually flatten the naked leg or hedge its delta immediately; this is
the highest-priority manual intervention.

## After any incident
1. Preserve logs and the audit trail.
2. Reconcile positions, cash, margin.
3. Write a short post-mortem: trigger, impact, root cause, fix.
4. Only then reset the kill switch (`KillSwitch.reset`) and resume per the
   start-up checklist in [OPERATIONS.md](OPERATIONS.md).
