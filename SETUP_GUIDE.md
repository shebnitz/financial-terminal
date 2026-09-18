# Setup Guide: VS Code, running this app, and Git/GitHub from zero

This walks through everything from "I have this folder of files" to
"the app is running on my screen" to "my code is backed up on GitHub."
Follow it in order the first time. Commands are shown for **Windows**
since that's what you're on; a Mac/Linux equivalent is noted where it
differs.

Do each numbered step, check the "you'll know it worked when" line, and
don't move on until it matches -- if something doesn't, that's exactly
where to stop and ask rather than pushing forward.

---

## Part 1 -- Get the project open in VS Code

### 1. Confirm Python is installed

Open a terminal (Windows: search "Terminal" or "PowerShell" in the
Start menu) and run:

```powershell
python --version
```

**You'll know it worked when** it prints something like
`Python 3.11.x` or `Python 3.12.x`. If instead you get an error, or it
opens the Microsoft Store, install Python from
[python.org/downloads](https://www.python.org/downloads/) first --
during install, check the box that says **"Add python.exe to PATH"**
before clicking Install. That checkbox is the single most common thing
beginners miss, and it's why `python` "isn't recognized" afterward.

### 2. Install VS Code (if you haven't)

Download it from [code.visualstudio.com](https://code.visualstudio.com/)
and install with defaults.

### 3. Install the Python extension in VS Code

Open VS Code -> click the Extensions icon in the left sidebar (looks
like four squares) -> search **"Python"** -> install the Microsoft one
(it's the top result, published by Microsoft). This is what gives VS
Code its "Run" button for Python files, debugging, and the environment
picker you'll use in a minute.

### 4. Open this folder in VS Code

In VS Code: **File -> Open Folder...** -> select the
`Financial Terminal` folder these files are in.

**You'll know it worked when** the file list on the left (the Explorer
panel) shows `app.py`, `sec_edgar.py`, `README.md`, etc.

### 5. Open the built-in terminal

**Terminal -> New Terminal** in the top menu (or `` Ctrl+` ``). This
opens a terminal already sitting *in* this project's folder -- notice
it, because every command below assumes you're here.

---

## Part 2 -- Set up a virtual environment and run the app

A **virtual environment** ("venv") is a private, isolated copy of
Python just for this project, so the packages it needs don't collide
with any other project's packages on your machine. Every real Python
project uses one; it's not optional busywork.

### 6. Create the virtual environment

```powershell
python -m venv .venv
```

This creates a `.venv` folder (already in `.gitignore`, so it never
gets committed to git -- it's regenerated from `requirements.txt`
instead, which is the whole point).

### 7. Activate it

```powershell
.venv\Scripts\Activate.ps1
```

*(Mac/Linux: `source .venv/bin/activate`)*

**You'll know it worked when** your terminal prompt gets a `(.venv)`
prefix. If PowerShell refuses with a script-execution error, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, answer yes,
then retry the activate command.

You'll need to run this activate command once per new terminal session
(VS Code will often offer to do it for you automatically -- accept
that when prompted).

### 8. Install the project's packages

```powershell
pip install -r requirements.txt
```

**You'll know it worked when** it ends with something like
`Successfully installed streamlit-... pandas-... requests-...` and no
red error text.

### 9. Set your SEC contact info

SEC EDGAR requires every request to include a real name + email (not a
secret, not an API key -- just an honest identifier, per SEC's access
rules). Copy the template file:

```powershell
copy .env.example .env
```

Open the new `.env` file in VS Code and replace the placeholder name
and email with your own. Save it. (`.env` is gitignored on purpose --
it stays on your machine only.)

### 10. Run the tests

```powershell
python -m pytest tests/ -v
```

**You'll know it worked when** you see `3 passed`. These tests don't
touch the network at all -- they check the data-parsing logic against
fake data, so they'll pass even offline.

### 11. Run the app

```powershell
streamlit run app.py
```

**You'll know it worked when** a browser tab opens automatically to
`localhost:8501` showing "Financial Terminal." Type `META` in the
sidebar and click **Fetch data**. First run may take a few seconds
while it downloads SEC's ticker list.

To stop the app, go back to the terminal and press `Ctrl+C`.

---

## Part 3 -- Git and GitHub, from scratch

**Git** is a tool that tracks the history of your code on your own
machine -- every "save point" (called a **commit**) you make, forever,
so you can always go back. **GitHub** is a website that hosts a copy of
that history online, so it's backed up and shareable. They're related
but different things: git works with no internet at all; GitHub is
just one (very popular) place to put a git project.

### 12. Install Git

Check if you already have it:

```powershell
git --version
```

If that errors, install from [git-scm.com](https://git-scm.com/downloads)
with the default options, then reopen your terminal.

### 13. Tell git who you are (one-time, ever)

```powershell
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

This is just a label attached to your commits -- separate from the
`.env` file from step 9, and it's fine for it to be the same email or a
different one.

### 14. Create a GitHub account

Go to [github.com](https://github.com/) and sign up (free) if you
haven't already.

### 15. Turn this folder into a git repository

Back in the VS Code terminal, in the project folder:

```powershell
git init
git add .
git commit -m "Initial commit: financial terminal app"
```

What just happened, in plain terms:
- `git init` -- starts tracking this folder with git.
- `git add .` -- stages every file (marks it "ready to be saved"),
  except anything listed in `.gitignore` (like `.venv/` and `.env` --
  check that `.env` is NOT in what got added; git will simply skip it).
- `git commit -m "..."` -- actually saves that snapshot, with a message
  describing it. You'll do `add` + `commit` again every time you want
  to save progress from here on.

**You'll know it worked when** `git commit` prints a summary like
`3 files changed` with no errors.

### 16. Create an empty repository on GitHub

On [github.com](https://github.com/), click the **+** in the top right
-> **New repository**. Name it `financial-terminal`, leave it
**Public** or **Private** (your choice), and -- important -- do
**not** check "Add a README" or any other initialize option, since you
already have files locally. Click **Create repository**.

GitHub will show you a page with commands under "...or push an existing
repository from the command line." Copy the two `git remote add` /
`git push` lines it gives you (they'll include your exact username and
repo name) -- they'll look like this:

```powershell
git remote add origin https://github.com/YOUR-USERNAME/financial-terminal.git
git branch -M main
git push -u origin main
```

### 17. Push your code

Run those three lines. The first time, it'll open a browser window
asking you to sign in to GitHub and authorize VS Code / git -- follow
that prompt.

**You'll know it worked when** refreshing the GitHub repository page in
your browser shows all your files.

### 18. Your ongoing workflow from here

Every time you make changes you want saved:

```powershell
git add .
git commit -m "describe what you changed"
git push
```

VS Code also has all of this built into the **Source Control** panel
(the icon in the left sidebar that looks like a branching line) if you'd
rather click than type: it shows changed files, has a message box and a
checkmark button for commit, and a "Sync Changes" button for push. Both
paths do the exact same thing -- use whichever feels more natural as
you get used to it.

---

## Where to go next

Once steps 1-17 all check out, you have a fully working, version
controlled Python project. From here, natural next steps (see the
"Extending it" section of `README.md`) are good small exercises: add
one new line item to a statement, or add an annual-vs-quarterly toggle
to the sidebar -- each one is a small, real change you can commit and
push on its own.
