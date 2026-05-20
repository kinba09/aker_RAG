import os
from sqlalchemy import create_engine


def get_engine():
    host = os.getenv('DB_HOST', 'mysql')
    port = os.getenv('DB_PORT', '3306')
    user = os.getenv('DB_USER', 'root')
    password = os.getenv('DB_PASSWORD', 'root')
    db = os.getenv('DB_NAME', 'property_chatbot')
    url = f"mysql+mysqlconnector://{user}:{password}@{host}:{port}/{db}"
    return create_engine(url, pool_pre_ping=True)
