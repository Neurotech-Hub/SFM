# sfm-analysis

Cross-platform analysis and printable HTML behavior reports for
[VFM](https://github.com/Neurotech-Hub/VFM) / SFM feeder session logs.
Runs on Windows, macOS, and Linux — no Raspberry Pi, no hardware libraries.

> Under construction: this package is being carved out of `tools/dev_gui/`
> in the parent repository. Full install and usage docs land alongside the
> first working CLI.

## Install

```bash
pip install sfm-analysis
```

Or, before the first PyPI release:

```bash
pip install "git+https://github.com/Neurotech-Hub/VFM.git#subdirectory=packages/sfm-analysis"
```

## Development

```bash
cd packages/sfm-analysis
pip install -e ".[dev]"
pytest
```
