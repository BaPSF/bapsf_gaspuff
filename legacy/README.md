# legacy

Archived gas-puff valve control code that drove the valve through a Siglent
arbitrary waveform generator. These files are retained for reference and are
**not** used by the current flow-meter system in `src/`.

Contents:

- `main.py` — Tk GUI for manual valve control
- `kernel.py` — `GasPuffValve` façade over the waveform generator
- `input.py` — square-wave waveform helper
- `wavegen_control.py` — Siglent SDG/SDS SCPI client used by the above

The four files import each other; they were moved together so the bundle
remains self-contained without source edits.
