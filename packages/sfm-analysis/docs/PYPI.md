# Publishing `sfm-analysis` to PyPI

How to ship a new version so `pip install sfm-analysis` picks it up.
Uploads go to **[pypi.org](https://pypi.org)** — not TestPyPI.

Package identity:


|              |                                                                                       |
| ------------ | ------------------------------------------------------------------------------------- |
| PyPI name    | `sfm-analysis`                                                                        |
| Import       | `sfm_analysis`                                                                        |
| Source       | `packages/sfm-analysis/` in [Neurotech-Hub/VFM](https://github.com/Neurotech-Hub/VFM) |
| Project page | [https://pypi.org/project/sfm-analysis/](https://pypi.org/project/sfm-analysis/)      |


`.github/workflows/sdk.yml` tests and builds the wheel; it does **not**
upload. Releases are cut from a laptop with Twine until trusted publishing
is wired up.

## 1. Bump the version

PyPI versions are immutable — you cannot overwrite `0.1.0`. Change **both**:

- `pyproject.toml` → `[project] version`
- `src/sfm_analysis/__init__.py` → `__version__`

Keep them identical. Then commit.

## 2. Token

1. Sign in at [pypi.org](https://pypi.org/) (not test.pypi.org).
2. [Account → API tokens](https://pypi.org/manage/account/token/).
3. **Add API token**.
  - First upload of a new project: scope **Entire account**.
  - Later releases: scope **Project: sfm-analysis**.
4. Copy the value. It starts with `pypi-`. Shown once.

If the Neurotech Hub organization should own the project, upload as a
member of that org (or transfer ownership after the first release:
project → Collaboration).

## 3. Build and upload

```bash
cd packages/sfm-analysis
python -m pip install --upgrade build twine
rm -rf dist
python -m build
twine check dist/*
twine upload dist/* \
  --username __token__ \
  --password 'pypi-PASTE_TOKEN_HERE'
```

- Username is exactly `__token__`.
- Password is the full token in single quotes.
- Do **not** pass `--repository testpypi`.
- Ignore `This environment is not supported for trusted publishing` — that
only applies to GitHub Actions, not a local Twine run.

Success prints a URL like `https://pypi.org/project/sfm-analysis/0.1.2/`.

## 4. Check the install

In a **fresh** venv (not the one you built from):

```bash
python -m venv /tmp/sfm-check && source /tmp/sfm-check/bin/activate
pip install --upgrade sfm-analysis
python -c "import sfm_analysis; print(sfm_analysis.__version__)"
sfm-report --list-designs
```

PyPI can take a minute to serve the new version. `pip install -U` if an
older copy is cached.

## 403 Forbidden

Almost always the wrong token or the wrong site.


| Cause                                          | Fix                                               |
| ---------------------------------------------- | ------------------------------------------------- |
| Token from **test.pypi.org**                   | Create a new token on **pypi.org**                |
| Account password instead of token              | Username `__token__`, password `pypi-...`         |
| Project-scoped token before the project exists | Entire-account token for the first upload         |
| Name already owned by another PyPI user        | Collaboration / org transfer, or a different name |


`twine upload dist/* --verbose` prints the real denial text.

## Tracking `main` without PyPI

```bash
pip install "git+https://github.com/Neurotech-Hub/VFM.git#subdirectory=packages/sfm-analysis"
```

The base-station GUI keeps using the local editable install
(`-e ../../packages/sfm-analysis` in `packages/dev_gui/requirements.txt`).
Leave that for development on the Pi; only pin `sfm-analysis>=X.Y` there
if the station should follow PyPI instead of the repo tree.