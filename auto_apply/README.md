# Auto-applier (Windows)

Reads the application packets from your morning **Job Digest** email and applies
to the Greenhouse, Lever and Ashby jobs for you: uploads your résumé, builds a
one-page cover letter PDF from your template, fills in the form, answers the
screening questions, and submits.

It **won't** get around CAPTCHAs or bot checks, and it won't guess at questions it
has no answer for. Those jobs, plus Workday and other sites, are left open in
browser tabs and listed under **Needs you** at the end, so you can finish them by hand.

## One-time setup

1. Install Python from <https://www.python.org/downloads/windows/>. On the first
   screen, check **"Add python.exe to PATH"**.
2. Put this folder somewhere permanent, e.g. `Documents\auto_apply`.
3. Make sure these are in the folder: `config.yaml`, `cover_letter_template.txt`,
   `IanFletcher-Resume.pdf`.
4. Double-click **setup.bat**. It installs everything, including its own browser.
5. Turn on 2-Step Verification for your Google account, then create an
   **App password** (Google Account → Security → App passwords). Paste it into
   `config.yaml`.
6. Open `config.yaml` in Notepad and fill in every `TODO`.

## Running it

- **test-run.bat** fills in everything but never clicks Submit. Use it first so
  you can check its work.
- **run.bat** does the real thing.

A browser window opens and you can watch it work. At the end you get a summary;
finish any tabs it left open, then press Enter to close.

Each job is only handled once (tracked in `applied.json`). To try the "Needs you"
ones again: `py apply.py --retry`.

## Running it automatically every evening (optional)

Open Command Prompt in this folder and run (change 18:30 to the time you want):

    schtasks /create /tn "Auto Apply" /tr "\"%cd%\run.bat\"" /sc daily /st 18:30

The laptop has to be on and logged in at that time.

## Files

- `apply.py`: the tool
- `config.yaml`: your info and answers (stays on your laptop, never uploaded)
- `output\`: the cover letter PDF made for each job
- `applied.json`: what's been submitted, skipped, or left for you
