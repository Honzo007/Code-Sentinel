# Code Sentinel

Code Sentinel checks a GitHub pull request before you merge it.

You paste the link of a pull request. An AI reviewer reads the code changes, looks for bugs and security problems, and tells you if the pull request looks safe to merge. It also gives a release-risk score from 0 to 100.

The app only reads the pull request. It never changes anything on GitHub.

## Features

- Live progress: four steps (Fetch, Bugs, Security, Summary) update as they run
- Bug check: logic errors, crashes, wrong conditions, missing error handling
- Security check: hardcoded secrets, injection, unsafe code, weak crypto
- Verdict: "Looks good to merge", "Worth a look" or "Changes needed", with a short summary
- Release-risk score from 0 to 100, with a table showing how it was calculated
- Each issue shows its severity, file, explanation and a suggested fix
- Download the review as a Markdown report
- Review history: the last 10 reviews are saved in your browser

## How it works

1. **Fetch:** gets the pull request details and code changes from GitHub (read-only).
2. **Bugs:** the AI looks at the changes for bugs only.
3. **Security:** a second AI request looks for security problems only.
4. **Summary:** a third AI request writes the verdict and summary.

The risk score is calculated in code, not by the AI: each high issue adds 25 points, medium adds 10, low adds 3, up to a maximum of 100. If any high issue exists, the verdict is always "Changes needed".

## Project structure

```
Code-Centinel/
  app.py             the whole app: server, GitHub and AI code, and the web page
  requirements.txt   the Python libraries to install
  .env.example       example settings (copy it to .env and add your key)
  .gitignore         files Git should not upload (.env, .venv)
  README.md          this file
```

`.env` is your private file with your API key. It is created by you and is never uploaded to GitHub.

## Run it

You need Python 3.10 or newer and a free Gemini API key from https://aistudio.google.com/apikey

```
git clone https://github.com/Honzo007/Code-Centinel.git
cd Code-Centinel
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Create a file named `.env` next to `app.py`:

```
GOOGLE_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-3.5-flash-lite
```

Start the app:

```
python app.py
```

Open http://127.0.0.1:5000, paste a public pull request link such as `https://github.com/owner/repo/pull/12`, and click **Analyze PR**.

## Settings

| Name | Required | Meaning |
|---|---|---|
| GOOGLE_API_KEY | Yes | Your Gemini API key |
| GEMINI_MODEL | No | The AI model name. Change it if Google retires a model. |
| GITHUB_TOKEN | No | A read-only GitHub token, only for private repositories |

## Built with

Python, Flask, GitHub REST API, Google Gemini (langchain-google-genai), python-dotenv, and plain HTML, CSS and JavaScript.
