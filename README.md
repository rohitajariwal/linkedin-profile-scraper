# LinkedIn Profile Extractor

A Python script that logs in to LinkedIn, extracts detailed profile information, and generates structured output in JSON and LaTeX/PDF format.

<img width="1918" height="1009" alt="image" src="https://github.com/user-attachments/assets/f3d405d7-8434-47aa-9255-3ef51833989f" />

## Features

- Extracts profile header details: name, headline, and location
- Extracts sections such as Experience, Education, Skills, Certifications, Projects, Volunteer Experience, Honors & Awards, Publications, and Languages
- Saves raw structured data as JSON
- Generates a LaTeX file and compiles it into a PDF
- Saves a debug HTML snapshot for troubleshooting if LinkedIn changes its layout

## Requirements

- Python 3.10+
- Google Chrome / Chromium compatible browser
- `pdflatex` or a LaTeX distribution such as MiKTeX / TeX Live

### Python dependencies

Install them with:

```bash
pip install -r requirements.txt
playwright install chromium
```

## Setup

```bash
git clone https://github.com/rohitajariwal/linkedin-profile-scraper.git
cd linkedin-profile-scraper-main
pip install -r requirements.txt
playwright install chromium
```

If PDF generation fails, install a LaTeX distribution:

- Windows: MiKTeX
- macOS: MacTeX
- Linux: TeX Live

## Usage

Run the script:

```bash
python linkedin_profile_extractor.py
```

Then:

1. Log in to LinkedIn in the opened browser window
2. Open your profile page
3. Scroll once to help load the content
4. Return to the terminal and press Enter

## Output files

The script creates these files in the working directory:

- `linkedin_profile.json` — structured extracted profile data
- `linkedin_profile.tex` — LaTeX source
- `linkedin_profile.pdf` — formatted PDF output
- `linkedin_profile_debug.html` — saved HTML snapshot for debugging

## Project structure

```text
.
├── linkedin_profile_extractor.py
├── requirements.txt
└── README.md
```

## Notes

- The script is designed for manual login and runs on pages you can access normally in your browser.
- LinkedIn may change its HTML structure at any time, so the debug HTML file is useful for future selector updates.
- Use responsibly and in line with LinkedIn’s terms and the permissions you have.
