import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

SEARCH_QUERIES = [
    'ዘማሪ',
    'ዲያቆን',
    'ethiopian orthodox mezmur',
    'የኦርቶዶክስ ዝማሬ',
    'diakon mezmur',
    'ethiopian orthodox tewahedo mezmur',
    'ዝማሬ',
    'orthodox mezmur new',
]

DOWNLOAD_DIR = 'downloads'
AUDIO_FORMAT = 'mp3'
AUDIO_QUALITY = '80'  
MAX_RESULTS_PER_QUERY = 3
CHECK_INTERVAL_MINUTES = 60

MIN_DURATION = 180
MAX_DURATION = 1800
