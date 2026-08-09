# Scripts

CLI / ops / backtest entrypoints. **Runtime code must not live here.**

Canonical domain packages:
- `services/exit_radar/` — evaluate_position, load_open_positions, sniper status
- `services/dca_sniper/` — standalone sniper
- `services/exit_radar_http.py` — Flask routes (imports domain, not scripts)

Conventions:
- `*backtest*` / `*retrospect*` — analysis, not production
- `railway_*` / `mongo_*` / `repair_*` — ops
- `gate_ws_live_dashboard.py` — probe UI only; domain in `services/exit_radar/`
