# Disk Space Reporter

See what is eating your disk space. Biggest folders, biggest files, space by type, and big files you haven't touched in a year.

**It only reads. It never deletes, moves or changes anything.**

No installs. Pure Python standard library. Runs only on your own PC, nothing is sent anywhere.

## How it works

1. Run it. A page opens in your browser.
2. Pick from the dropdown (**Whole C: drive**, **User folder**, then any other drives like D: or a USB stick), or type any folder in the box, then press **Scan**.
3. Read the report. Use **Download biggest files (CSV)** if you want a spreadsheet.

The report has:

- Space by type (video, pictures, documents, archives and so on)
- The folders inside the one you scanned, biggest first
- The biggest folders deeper down
- The biggest files
- Big files (100 MB+) you haven't touched in a year, which are usually the safest ones to move or delete yourself

## Good to know

- A whole-drive scan takes longer. Run it as administrator to include system folders. Anything it can't read is counted and shown, not skipped silently.
- Links and junctions are never followed, so nothing is counted twice. Cloud-only files (OneDrive and similar) take no space on your PC, so they're skipped too.
- The page only works on 127.0.0.1 and needs a random token, so other websites can't use it.

## Run it

**Windows:** double-click `Run Disk Space Reporter.bat`

**Terminal:**

```
python disk_space_reporter.py                       # open the page
python disk_space_reporter.py "D:\Games"            # open the page with that folder filled in
python disk_space_reporter.py --text "C:\Users"     # report in the terminal, no page
python disk_space_reporter.py --text . --csv big.csv --top 50
```

Needs Python 3.8 or newer.

## Tests

```
python -m unittest -v
```

## License

MIT
