# Learnings

Corrections, insights, and knowledge gaps captured during development.

**Categories**: correction | insight | knowledge_gap | best_practice

---

## [LRN-20260715-001] Windows batch file encoding (best_practice)

**Logged**: 2026-07-15T16:28:00+08:00
**Priority**: high
**Status**: pending
**Area**: config

### Summary
`.bat` files saved as UTF-8 with Chinese characters fail when cmd.exe reads them.

### Details
cmd.exe uses ANSI (GBK/CP936) encoding, not UTF-8. Hardcoded Chinese paths like `C:\Users\王兴锋\...` in `.bat` files will be corrupted. Always use `%USERPROFILE%` environment variable instead.

### Suggested Action
When writing `.bat` files that reference user paths, use `%USERPROFILE%` never hardcode Chinese characters.

### Metadata
- Source: error
- Related Files: `scripts/update_data.bat`
- Tags: windows, encoding, batch
- Pattern-Key: windows.bat.encoding

---

## [LRN-20260715-002] Windows batch file line endings (best_practice)

**Logged**: 2026-07-15T16:28:00+08:00
**Priority**: high
**Status**: pending
**Area**: config

### Summary
`.bat` files must use CRLF (`\r\n`) line endings, not LF (`\n`).

### Details
cmd.exe cannot parse LF-only line endings correctly. Lines get concatenated, commands become garbled. Exit code 255 in Windows Task Scheduler is a common symptom. The `file` command shows "DOS batch file, UTF-8 text" for CRLF files, and "Unicode text, UTF-8 text" for LF files.

### Suggested Action
Always ensure `.bat` files are saved with CRLF line endings. Use `python -c "open('file.bat', 'w', newline='').write(content)"` to write with CRLF from Python.

### Metadata
- Source: error
- Related Files: `scripts/update_data.bat`
- Tags: windows, batch, line-endings
- Pattern-Key: windows.bat.line_endings

---

## [LRN-20260715-003] Streamlit number_input min_value default (correction)

**Logged**: 2026-07-15T16:28:00+08:00
**Priority**: high
**Status**: pending
**Area**: frontend

### Summary
`st.number_input` with hardcoded `min_value=0.0` for float parameters will crash on negative defaults.

### Details
In `app/backtest.py`, the float parameter branch used `st.number_input(desc, value=default, min_value=0.0)`. The multi-factor strategy's `sell_threshold=-3.0` triggered `StreamlitValueBelowMinError`. Fix: don't set `min_value` for float parameters, let it default to None.

### Suggested Action
When building Streamlit parameter UIs, don't impose `min_value`/`max_value` unless the parameter schema explicitly defines bounds.

### Metadata
- Source: error
- Related Files: `app/backtest.py`
- Tags: streamlit, number_input, validation
- Pattern-Key: streamlit.number_input.min_value

---

## [LRN-20260715-004] BacktestResult missing field (correction)

**Logged**: 2026-07-15T16:28:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
Displaying a new metric (`calmar_ratio`) in the backtest result view crashed with `AttributeError` because the `BacktestResult` dataclass didn't have the field.

### Details
`Portfolio.calc_metrics()` returns `calmar_ratio` in the metrics dict, but `BacktestResult` model didn't have it, and `Backtester.run()` didn't pass it through. Fix: added `calmar_ratio: float = 0.0` to the dataclass and populated it in `run()`.

### Suggested Action
When adding new metrics to the display, always: 1) add field to the model, 2) populate it in the backtester, 3) verify `hasattr` or ensure field exists.

### Metadata
- Source: error
- Related Files: `core/models.py`, `engine/backtester.py`
- Tags: model, backtest, attribute-error
- Pattern-Key: backtest.model.missing_field

---

## [LRN-20260715-005] Numpy float64 index after groupby (knowledge_gap)

**Logged**: 2026-07-15T16:28:00+08:00
**Priority**: medium
**Status**: pending
**Area**: backend

### Summary
After pandas groupby+reset_index, column values accessed via `iterrows()` may be `numpy.float64` even when the dtype shows `int32`.

### Details
`mi = row["month"] - 1` returned `numpy.float64(0.0)` instead of `int`. Numpy arrays cannot be indexed with float values, causing `IndexError`. Fix: `mi = int(row["month"]) - 1`.

### Suggested Action
Always convert groupby result column values to plain Python `int` with `int(val)` before using as numpy array indices.

### Metadata
- Source: error
- Related Files: `app/backtest.py`
- Tags: numpy, pandas, groupby, indexing
- Pattern-Key: numpy.index.float64

---

## [LRN-20260715-006] SQLite INSERT OR REPLACE for unique constraints (best_practice)

**Logged**: 2026-07-15T16:28:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary
`save_signal` used plain `INSERT INTO` but the `signal` table has a `UNIQUE(ts_code, trade_date, strategy, direction)` constraint, causing `IntegrityError` on repeated scans.

### Details
The scanner runs multiple times and generates the same signals. Using `INSERT OR REPLACE` handles duplicates gracefully by updating the existing record with the latest values (score, reason, price_ref).

### Suggested Action
When a table has a UNIQUE constraint and the operation is "upsert", use `INSERT OR REPLACE` instead of `INSERT`.

### Metadata
- Source: error
- Related Files: `data/storage.py`
- Tags: sqlite, constraint, signal
- Pattern-Key: sqlite.insert_or_replace