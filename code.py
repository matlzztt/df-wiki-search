import os
import xml.etree.ElementTree as ET
import sqlite3
import re

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(PROJECT_DIR, "df_wiki.db")
XML_FILE = os.path.join(PROJECT_DIR, "df_wiki_compressed.xml")

def setup_database(conn=None):
    if conn is None:
        conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Create the standard structured data table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS wiki_articles (
        id INTEGER PRIMARY KEY,  -- SQLite handles auto-incrementing natively here
        title TEXT UNIQUE,
        namespace TEXT,
        body_content TEXT,
        last_modified TEXT
    );
    """)
    
    # Create the FTS5 Virtual Table for full-text search
    cursor.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS wiki_search_index USING fts5(
        title,
        body_content,
        content='wiki_articles',
        content_rowid='id'
    );
    """)
    
    # Triggers to keep the FTS5 Index automatically in sync with main table updates
    cursor.execute("""
    CREATE TRIGGER IF NOT EXISTS after_wiki_insert AFTER INSERT ON wiki_articles BEGIN
        INSERT INTO wiki_search_index(rowid, title, body_content) 
        VALUES (new.id, new.title, new.body_content);
    END;
    """)
    
    conn.commit()
    return conn

def sanitize_wiki_text(raw_text):
    """
    Cleans up junk wiki templates, media links, and excessive syntax 
    to maximize Claude's token efficiency.
    """
    if not raw_text:
        return ""
    
    # 1. Strip Wikipedia-style metadata/quality headers e.g., {{quality|...}}
    cleaned = re.sub(r'\{\{[^\|\}]+\|.*?\}\}', '', raw_text)
    cleaned = re.sub(r'\{\{[^\}]+\}\}', '', cleaned)
    
    # 2. Simplify Internal Wiki Links: [[Category:Guides]] -> Guides
    cleaned = re.sub(r'\[\[(?:[^|\]]*\|)?([^\]]+)\]\]', r'\1', cleaned)
    
    # 3. Strip structural artifacts like Table of Contents tokens
    cleaned = cleaned.replace("__TOC__", "")
    
    return cleaned.strip()

def ensure_database_ready():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wiki_articles'")
    has_articles = cursor.fetchone() is not None
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wiki_search_index'")
    has_index = cursor.fetchone() is not None

    if not has_articles or not has_index:
        setup_database(conn)
        parse_and_populate(conn)

    conn.close()


def parse_and_populate(conn=None):
    if conn is None:
        conn = setup_database()
    else:
        setup_database(conn)

    cursor = conn.cursor()
    
    # Stream the XML file page by page to avoid memory spikes
    context = ET.iterparse(XML_FILE, events=('end',))
    
    success_count = 0
    
    for event, elem in context:
        # We only care when a full <page> element has finished parsing
        if elem.tag.endswith('page'):
            title_node = elem.find('.//{*}title')
            timestamp_node = elem.find('.//{*}timestamp')
            text_node = elem.find('.//{*}text')
            
            if title_node is not None and text_node is not None:
                raw_title = title_node.text
                raw_text = text_node.text
                timestamp = timestamp_node.text if timestamp_node is not None else ""
                
                # Deduce Namespace (e.g., "DF2012:Starting build" -> "DF2012")
                if ":" in raw_title:
                    namespace = raw_title.split(":")[0]
                else:
                    namespace = "Main"
                
                clean_text = sanitize_wiki_text(raw_text)
                
                try:
                    cursor.execute("""
                        INSERT OR REPLACE INTO wiki_articles (title, namespace, body_content, last_modified)
                        VALUES (?, ?, ?, ?)
                    """, (raw_title, namespace, clean_text, timestamp))
                    success_count += 1
                except sqlite3.Error as e:
                    print(f"Error skipping page '{raw_title}': {e}")
            
            # Crucial: Clear out elements from memory once processed
            elem.clear()
            
    conn.commit()
    conn.close()
    print(f"Database build complete. Successfully indexed {success_count} pages.")

if __name__ == "__main__":
    parse_and_populate()