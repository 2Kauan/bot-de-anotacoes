import datetime
import os.path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/calendar']

def get_service():
    creds = None

    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    else:
        flow = InstalledAppFlow.from_client_secrets_file(
            'credentials.json', SCOPES)
        creds = flow.run_local_server(port=0)

        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    service = build('calendar', 'v3', credentials=creds)
    return service


def create_test_event():
    service = get_service()

    now = datetime.datetime.now() + datetime.timedelta(minutes=2)

    event = {
        'summary': 'TESTE BOT TELEGRAM',
        'start': {
            'dateTime': now.isoformat(),
            'timeZone': 'America/Sao_Paulo',
        },
        'end': {
            'dateTime': (now + datetime.timedelta(hours=1)).isoformat(),
            'timeZone': 'America/Sao_Paulo',
        },
    }

    event = service.events().insert(
        calendarId='primary',
        body=event
    ).execute()

    print("Evento criado com sucesso!")
    print("Link:", event.get('htmlLink'))

create_test_event()