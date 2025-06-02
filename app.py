from functools import lru_cache
from flask import Flask, request, render_template
from google.oauth2 import service_account
from googleapiclient.discovery import build, build as build_docs_api
from googleapiclient.discovery import build as build_sheets_api
import json
from flask import jsonify

app = Flask(__name__)

#Google API setup
SERVICE_ACCOUNT_FILE = "service-account.json"
SCOPES = ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/documents"]

creds = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)

#Google Drive API
drive_service = build("drive", "v3", credentials=creds)

#Google Docs API(will be reused later)
docs_service = build_docs_api("docs", "v1", credentials=creds)

def get_available_folders():
    query = "mimeType='application/vnd.google-apps.folder' and trashed = false"
    response = drive_service.files().list(
        q=query,
        fields="files(id, name, parents)",
        pageSize=1000
    ).execute()

    folders = response.get("files", [])
    folder_map = {f["id"]: f for f in folders}

    def build_trimmed_path(folder):
        parts = [folder["name"]]  # start with current folder name
        current = folder

        while "parents" in current:
            parent_id = current["parents"][0]
            parent = folder_map.get(parent_id)
            if not parent:
                break

#Skip "SEO" folders in the middle
            if parent["name"].lower() != "seo":
                parts.insert(0, parent["name"])
            current = parent

        return " > ".join(parts)

    structured_folders = []

    for folder in folders:
        if folder["name"].strip().lower() == "technical seo":
            structured_folders.append({
                "id": folder["id"],
                "name": build_trimmed_path(folder)
            })

    structured_folders.sort(key=lambda x: x["name"].lower())
    return structured_folders

def get_developer_briefs_subfolders():
    query = f"mimeType='application/vnd.google-apps.folder' and trashed = false and '{DEVELOPER_BRIEFS_FOLDER_ID}' in parents"
    response = drive_service.files().list(q=query, fields="files(id, name)", pageSize=100).execute()
    return response.get("files", [])

DEVELOPER_BRIEFS_FOLDER_ID = "1yIVCHdVChV1VjDT40MAweIaChyhhw0cv"  # Replace with your real ID


def get_folder_metadata_map(folder_ids):
    query = " or ".join([f"'{{fid}}' in parents" for fid in folder_ids])
    response = drive_service.files().list(
        q=f"mimeType='application/vnd.google-apps.folder' and trashed = false and ({{query}})",
        fields="files(id, name, parents)",
        pageSize=1000
    ).execute()
    return {{f["id"]: f for f in response.get("files", [])}}


def search_docs(keyword=None, folder_filter=None):
    if folder_filter:
        # Limit to the selected subfolder and its children
        all_folder_ids = get_all_folder_ids_under(folder_filter)
    else:
        # Search all folders under Developer Briefs
        all_folder_ids = get_all_folder_ids_under(DEVELOPER_BRIEFS_FOLDER_ID)

    folder_conditions = " or ".join([f"'{fid}' in parents" for fid in all_folder_ids])
    base_query = f"mimeType='application/vnd.google-apps.document' and trashed = false and ({folder_conditions})"

    if keyword:
        query = f"{base_query} and fullText contains '{keyword}'"
    else:
        query = base_query

#Fetch matching documents
    results = drive_service.files().list(
        q=query,
        fields="files(id, name, modifiedTime, webViewLink, parents)",
        orderBy="modifiedTime desc"
    ).execute()

    files = results.get("files", [])

#Step 1 : Get metadata for all folders
    folder_metadata = {}
    for fid in all_folder_ids:
        try:
            folder = drive_service.files().get(fileId=fid, fields="id, name, parents").execute()
            folder_metadata[fid] = folder
        except:
            continue

#Step 2 : Build full folder path
    def build_folder_path(folder_id, folder_map):
        path_parts = []
        current_id = folder_id

        while current_id in folder_map:
            folder = folder_map[current_id]
            path_parts.insert(0, folder["name"])
            if "parents" in folder:
                current_id = folder["parents"][0]
            else:
                break

        return " > ".join(path_parts)

#Step 3 : Attach folder path to each doc
    for file in files:
        parent_id = file.get("parents", [None])[0]
        if parent_id:
            full_path = build_folder_path(parent_id, folder_metadata)
            file["folderName"] = full_path
        else:
            file["folderName"] = "Unknown Location"

    return files

#Step 1 : Find all sub - folder IDs recursively under "Developer Briefs"
@lru_cache(maxsize=1)
def get_all_folder_ids_under(parent_id):
    folder_ids = [parent_id]  # start with Developer Briefs
    query = "mimeType='application/vnd.google-apps.folder' and trashed = false and '{0}' in parents".format(parent_id)
    
    results = drive_service.files().list(q=query, fields="files(id)").execute()
    folders = results.get("files", [])

    for folder in folders:
        folder_ids.extend(get_all_folder_ids_under(folder["id"]))  # recursive

    return folder_ids


SPREADSHEET_ID = "19ykdZU6LG3NfttY3TANqkTMoh0Bi_b22QHIlqhznbto"
SHEET_NAME = "Accounts"

def get_client_data():
    sheets_service = build_sheets_api("sheets", "v4", credentials=creds)
    range_ = f"{SHEET_NAME}!A4:R"  # Covers all rows down to column R

    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=range_
    ).execute()

    values = result.get("values", [])
    client_data = {}

    for row in values:
        if len(row) >= 2 and row[0].strip():
            client_name = row[0].strip()
            client_data[client_name] = {
                "lead": row[1] if len(row) > 1 else "",
                "tickets": row[15] if len(row) > 15 else "",
                "completed": row[16] if len(row) > 16 else "",
                "percent": row[17] if len(row) > 17 else ""
            }

    return client_data

#Homepage search + results
@app.route("/", methods=["GET", "POST"])
def home():
    results = None
    keyword = None
    folders = get_available_folders()
    client_data_dict = get_client_data()
    search_folders = get_developer_briefs_subfolders()
    client_names = sorted(client_data_dict.keys())  # List of just names for dropdown

    if request.method == "POST":
        keyword = request.form.get("keyword")
        view_all = request.form.get("view_all")

        folder_filter = request.form.get("folder_filter") or None
        results = search_docs(keyword if not view_all else None, folder_filter)

        return render_template(
            "index.html",
            results=results,
            keyword=keyword,
            folders=folders,
            clients=client_names,           # Dropdown list
            client_data=json.dumps(client_data_dict),  # JSON for JS
            search_folders=search_folders
        )

    return render_template(
        "index.html",
        results=None,
        keyword=None,
        folders=folders,
        clients=client_names,
        client_data=json.dumps(client_data_dict),
        search_folders=search_folders
    )

@app.route("/create-brief", methods=["POST"])
def create_brief():
#Step 1 : Get form data
    file_id = request.form.get("file_id")
    client_name = request.form.get("client_name")
    ref_no = request.form.get("ref_no")
    issue_category = request.form.get("issue_category")
    issue_name = request.form.get("issue_name")
    priority = request.form.get("priority")
    folder_id = request.form.get("folder_id")  # NEW: selected folder

#Step 2 : Create new document name
    new_title = f"{ref_no} // {issue_name}"

#Step 3 : Copy the original Google Doc
    copied_file = drive_service.files().copy(
        fileId=file_id,
        body={
            "name": new_title,
            "parents": [folder_id]  # NEW: save to selected folder
        }
    ).execute()

    new_file_id = copied_file["id"]

#Step 4 : Set placeholder replacements
    replacements = {
        "<<Client Name>>": client_name,
        "<<Ref No>>": ref_no,
        "<<Issue Category>>": issue_category,
        "<<Issue Name>>": issue_name,
        "<<Priority>>": priority,
    }

#Step 5 : Build Google Docs API request
    requests = []
    for placeholder, value in replacements.items():
        requests.append({
            "replaceAllText": {
                "containsText": {
                    "text": placeholder,
                    "matchCase": True
                },
                "replaceText": value
            }
        })

#Step 6 : Apply replacements using Docs API
    docs_service.documents().batchUpdate(
        documentId=new_file_id,
        body={"requests": requests}
    ).execute()

#Step 7 : Link to new document
    doc_link = f"https://docs.google.com/document/d/{new_file_id}/edit"

    return jsonify({"doc_link": doc_link})

if __name__ == "__main__":
    app.run(debug=True)