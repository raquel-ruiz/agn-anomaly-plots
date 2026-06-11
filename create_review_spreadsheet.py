"""
create_review_spreadsheet.py

Creates ONE Google Spreadsheet with 13 tabs (12 OOD model tabs + 1
contextual tab) from the 13 CSVs produced by build_anomaly_sheets.py.

The spreadsheet is pure data: no embedded images. Each tab gets the
CSV columns plus the existing 'interesting' and 'obs' columns (already
in the CSVs as 'interesting'/'obs' if you kept INSPECTION_COLS, here we
just make sure 'interesting' has a yes/no/maybe data-validation dropdown
so the values stay consistent with what the web app writes).

The Apps Script web app (separate, deployed at script.google.com) is
what you actually use to review; it reads/writes these tabs directly.
This script only builds the data backend.

Prerequisites:
  pip install gspread google-auth
  A Google Cloud service account JSON key (see setup instructions).
  Google Sheets API and Google Drive API enabled on the project.

Run on your machine (needs the CSVs and the credentials JSON).
"""

import os
import glob
import time

import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError


# ----------------------------------------------------------------------
# CONFIG  -- adjust to your setup
# ----------------------------------------------------------------------
SHEETS_DIR = '../AGN_AD/anomaly_sheets'          # the 13 CSVs
CREDENTIALS_JSON = 'google_credentials.json'     # service account key path

# The script does NOT create the spreadsheet (service accounts have no
# Drive storage quota, so creation fails with a misleading error).
# Instead: create an empty Google Sheet manually in your Drive, share it
# with the service account email as Editor, and put its id here.
# The id is the part of the URL between /d/ and /edit:
#   https://docs.google.com/spreadsheets/d/THIS_PART/edit
SPREADSHEET_ID = '1QoHqTJEcx7oZnjqSVEbQWQ_VZGZ3Wa5QGMx1fVDeEvM'

# Tab order: 12 OOD models first, contextual last.
ALGS = ['if', 'svm']
AGN_CLASSES = ['core', 'host', 'typ2']
FEATURES = ['latent', 'latent_var']
OOD_TABS = [f'{a}_{c}_{f}'
            for a in ALGS for c in AGN_CLASSES for f in FEATURES]
CONTEXTUAL_TAB = 'contextual'
TAB_ORDER = OOD_TABS + [CONTEXTUAL_TAB]

# classification dropdown values (must match what the web app writes)
CLASSIFICATION_VALUES = ['yes', 'no']

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def gc_client():
    creds = Credentials.from_service_account_file(
        CREDENTIALS_JSON, scopes=SCOPES)
    return gspread.authorize(creds)


def load_csv(tab_name):
    """Loads the CSV for a tab, as strings (so big oid_g aren't mangled)."""
    path = os.path.join(SHEETS_DIR, f'{tab_name}.csv')
    if not os.path.exists(path):
        raise FileNotFoundError(f'Missing CSV: {path}')
    # keep everything as string for the upload; numbers display fine and
    # the 18-digit oid_g never loses precision this way
    df = pd.read_csv(path, dtype=str).fillna('')
    return df


def col_letter(n):
    """1-based column index -> spreadsheet letter(s). 1->A, 27->AA."""
    s = ''
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def with_retry(fn, *args, **kwargs):
    """
    Google API sometimes returns 429/500. Retry with backoff so a long
    upload doesn't die on a transient hiccup.
    """
    delay = 5
    for attempt in range(6):
        try:
            return fn(*args, **kwargs)
        except APIError as e:
            if attempt == 5:
                raise
            print(f'    API error ({e}), retrying in {delay}s...')
            time.sleep(delay)
            delay *= 2


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main():
    if 'PASTE_THE_SPREADSHEET_ID_HERE' in SPREADSHEET_ID:
        raise SystemExit(
            'Create an empty Google Sheet manually, share it with the '
            'service account as Editor, and set SPREADSHEET_ID at the top.')

    # sanity: all 13 CSVs present
    found = sorted(os.path.basename(p)
                   for p in glob.glob(os.path.join(SHEETS_DIR, '*.csv')))
    print(f'## Found {len(found)} CSVs in {SHEETS_DIR}')
    missing = [f'{t}.csv' for t in TAB_ORDER if f'{t}.csv' not in found]
    if missing:
        raise SystemExit(f'Missing expected CSVs: {missing}')

    gc = gc_client()

    # open the existing spreadsheet (created and shared by you)
    print(f'## Opening spreadsheet {SPREADSHEET_ID}...')
    ss = with_retry(gc.open_by_key, SPREADSHEET_ID)
    print(f'## URL: https://docs.google.com/spreadsheets/d/{ss.id}')

    # the spreadsheet starts with one default sheet; we rename it to the
    # first tab and create the rest. Any pre-existing extra sheets are
    # left alone.
    # map of existing tabs by title, so a rerun reuses them instead of
    # failing on duplicate creation
    existing = {ws.title: ws for ws in ss.worksheets()}
    default_ws = ss.sheet1

    for i, tab in enumerate(TAB_ORDER):
        print(f'## [{i+1}/{len(TAB_ORDER)}] Tab: {tab}')
        df = load_csv(tab)
        n_rows, n_cols = df.shape
        print(f'   {n_rows} rows x {n_cols} cols')

        if tab in existing:
            # tab already exists: clear it and reuse it
            ws = existing[tab]
            with_retry(ws.clear)
            with_retry(ws.resize, rows=n_rows + 1, cols=n_cols)
        elif i == 0 and default_ws.title not in TAB_ORDER:
            # first tab and the default sheet is still unnamed-as-a-tab:
            # repurpose the default sheet
            ws = default_ws
            with_retry(ws.update_title, tab)
            with_retry(ws.resize, rows=n_rows + 1, cols=n_cols)
            existing[tab] = ws
        else:
            ws = with_retry(ss.add_worksheet, title=tab,
                            rows=n_rows + 1, cols=n_cols)
            existing[tab] = ws

        # write header + data in one batch (values as a list of lists)
        values = [list(df.columns)] + df.values.tolist()
        with_retry(ws.update, values, 'A1',
                   value_input_option='USER_ENTERED')

        # find the 'interesting' column to attach the dropdown
        if 'interesting' in df.columns:
            ci = list(df.columns).index('interesting') + 1  # 1-based
            letter = col_letter(ci)
            rng = f'{letter}2:{letter}{n_rows + 1}'
            # data validation: one-of CLASSIFICATION_VALUES
            with_retry(set_dropdown, ss, ws, rng, CLASSIFICATION_VALUES)

        # freeze the header row
        with_retry(ws.freeze, rows=1)
        time.sleep(1)  # be gentle with the API between tabs

    # if the spreadsheet had a leftover default sheet not in TAB_ORDER
    # (e.g. "Sheet1" from manual creation), remove it so only the 13
    # data tabs remain
    for ws in ss.worksheets():
        if ws.title not in TAB_ORDER:
            print(f'## Removing leftover sheet: {ws.title}')
            with_retry(ss.del_worksheet, ws)

    print('\n## Done.')
    print(f'## Open: https://docs.google.com/spreadsheets/d/{ss.id}')
    print('## Next: deploy the Apps Script web app pointed at this id.')


def set_dropdown(ss, ws, a1_range, values):
    """
    Adds a data-validation dropdown (one of `values`) to a range,
    using the low-level batch_update (gspread has no direct helper).
    """
    # resolve the range to GridRange
    start_cell, end_cell = a1_range.split(':')
    start = gspread.utils.a1_to_rowcol(start_cell)
    end = gspread.utils.a1_to_rowcol(end_cell)
    grid_range = {
        'sheetId': ws.id,
        'startRowIndex': start[0] - 1,
        'endRowIndex': end[0],
        'startColumnIndex': start[1] - 1,
        'endColumnIndex': end[1],
    }
    request = {
        'setDataValidation': {
            'range': grid_range,
            'rule': {
                'condition': {
                    'type': 'ONE_OF_LIST',
                    'values': [{'userEnteredValue': v} for v in values],
                },
                'showCustomUi': True,
                'strict': False,
            },
        }
    }
    ss.batch_update({'requests': [request]})


if __name__ == '__main__':
    main()
