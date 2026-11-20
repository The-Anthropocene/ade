## Which file do I download?

| Your computer | Download |
|---|---|
| Mac with Apple silicon (M1 or newer) | `atlas-ade-{version}-macos-arm64.tar.gz` |
| Mac with an Intel chip | `atlas-ade-{version}-macos-x64.tar.gz` |
| Linux (x86-64) | `atlas-ade-{version}-linux.tar.gz` |
| Windows (x86-64) | `atlas-ade-{version}-windows.zip` |

Not sure which Mac you have? Open the Apple menu, then About This Mac. "Chip: Apple M..." means Apple silicon; "Processor: ... Intel" means Intel.

Unpack it and follow `INSTALL.txt` inside, or the [install guide](https://atlas-ade.dev/docs/install/). Already use Python? `uv tool install atlas-ade` or `pipx install atlas-ade` does the same job without a download.

## What are the other files?

- **`.sha256`**: a fingerprint of the download beside it. If your copy gives the same fingerprint, it arrived whole and unchanged. It shows the file matches what this page published; it is not a publisher signature.
- **`mingit-...-source.tar.gz`**: the source code of the small Git that ships inside the Windows download. Its licence asks us to publish it. You don't need it to use ADE.
- **Source code (zip, tar.gz)**: GitHub adds these to every release. They are ADE's source code, not a ready-to-run download.

## How to check a download

Put the download and its `.sha256` file in the same folder, open a terminal in that folder, and run the line for your system.

- **macOS:** `shasum -a 256 -c atlas-ade-{version}-macos-arm64.tar.gz.sha256` (use the `x64` name on an Intel Mac). It prints `OK`.
- **Linux:** `sha256sum -c atlas-ade-{version}-linux.tar.gz.sha256`. It prints `OK`.
- **Windows (PowerShell):** `(Get-FileHash atlas-ade-{version}-windows.zip).Hash` prints a long code. Open the `.sha256` file in Notepad: the code at its start should be the same (capital letters don't matter).

Anything other than `OK` or a matching code means the download is damaged or not ours: delete it and download it again.
