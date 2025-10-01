import time
import psycopg2
import numpy as np
from sentence_transformers import SentenceTransformer
from bs4 import BeautifulSoup
from datetime import datetime
import json  
from urllib.parse import urlparse

# Initialize embedding model
model = SentenceTransformer("all-MiniLM-L6-v2")

# Vector Database (PostgreSQL with pgvector) Connection
DB_CONFIG = {
    "dbname": "vector_database",
    "user": "test_case_validator",
    "password": "",
    "host": "localhost"
}

PG_CONN = None
PG_CURSOR = None

def connect_to_db():
    global PG_CONN, PG_CURSOR
    try:
        PG_CONN = psycopg2.connect(**DB_CONFIG)
        PG_CURSOR = PG_CONN.cursor()
        return True
    except Exception as e:
        print(f"Error connecting to PostgreSQL: {e}")
        return False

def _common_url(u: str) -> str:
    """Extract domain (netloc) from full URL."""
    try:
        host = urlparse(u).netloc or ""
        return host.lower()
    except Exception:
        return ""

def init_database():
    """
    Initialize DB schema. Adds 'page_actions' JSONB column and 'common_url' column.
    """
    global PG_CONN, PG_CURSOR
    if not PG_CONN or not PG_CURSOR:
        if not connect_to_db():
            return False
    try:
        PG_CURSOR.execute("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'vector')")
        has_vector = PG_CURSOR.fetchone()[0]
        if not has_vector:
            PG_CURSOR.execute("CREATE EXTENSION IF NOT EXISTS vector")

        PG_CURSOR.execute("""
        CREATE TABLE IF NOT EXISTS page_embeddings (
            url TEXT PRIMARY KEY, 
            embedding vector(384), 
            content TEXT, 
            content_type TEXT DEFAULT 'html',
            timestamp FLOAT,
            title TEXT,
            is_alert BOOLEAN DEFAULT false,
            session_id TEXT,
            page_actions JSONB DEFAULT '{"actions":[]}'::jsonb,
            common_url TEXT
        );
        """)

        # Ensure page_actions column exists
        PG_CURSOR.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='page_embeddings' AND column_name='page_actions'
            ) THEN
                ALTER TABLE page_embeddings
                ADD COLUMN page_actions JSONB DEFAULT '{"actions":[]}'::jsonb;
            END IF;
        END$$;
        """)

        # Ensure common_url column exists
        PG_CURSOR.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='page_embeddings' AND column_name='common_url'
            ) THEN
                ALTER TABLE page_embeddings
                ADD COLUMN common_url TEXT;
            END IF;
        END$$;
        """)

        PG_CURSOR.execute("""
        CREATE INDEX IF NOT EXISTS page_embeddings_embedding_idx 
        ON page_embeddings USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100);
        """)
        PG_CURSOR.execute("""
        CREATE INDEX IF NOT EXISTS page_embeddings_session_idx 
        ON page_embeddings (session_id);
        """)
        PG_CURSOR.execute("""
        CREATE INDEX IF NOT EXISTS page_embeddings_actions_gin 
        ON page_embeddings USING GIN (page_actions);
        """)
        PG_CURSOR.execute("""
        CREATE INDEX IF NOT EXISTS page_embeddings_common_url_idx
        ON page_embeddings (common_url);
        """)

        PG_CONN.commit()
        return True
    except Exception as e:
        print(f"Error setting up vector DB: {e}")
        PG_CONN.rollback()
        return False

def store_in_pgvector(url, content, metadata=None, session_id=None, page_actions=None):
    """
    Store page content and embeddings; optionally upsert page_actions JSON and common_url.
    """
    global PG_CONN, PG_CURSOR
    
    if not PG_CONN or not PG_CURSOR:
        if not connect_to_db():
            return False
    
    try:
        if content and isinstance(content, str):
            if metadata and metadata.get("is_alert", False):
                text_content = content
                content_type = "alert"
            else:
                soup = BeautifulSoup(content, "html.parser")
                text_content = soup.get_text().strip()
                content_type = "html"
        else:
            text_content = "No content available"
            content_type = "unknown"
        
        embedding = model.encode(text_content).astype('float32')
        
        title = metadata.get("title", "") if metadata else ""
        is_alert = metadata.get("is_alert", False) if metadata else False
        
        if not session_id:
            from database.history_manager import history_manager
            if history_manager.current_session:
                session_id = history_manager.current_session.id

        common_url = _common_url(url)

        PG_CURSOR.execute("""
            INSERT INTO page_embeddings 
                (url, embedding, content, content_type, timestamp, title, is_alert, session_id, page_actions, common_url) 
            VALUES 
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (url) DO UPDATE 
            SET embedding      = EXCLUDED.embedding,
                content        = EXCLUDED.content,
                content_type   = EXCLUDED.content_type,
                timestamp      = EXCLUDED.timestamp,
                title          = EXCLUDED.title,
                is_alert       = EXCLUDED.is_alert,
                session_id     = EXCLUDED.session_id,
                page_actions   = COALESCE(EXCLUDED.page_actions, page_embeddings.page_actions),
                common_url     = EXCLUDED.common_url;
        """, (
            url,
            embedding.tolist(),
            text_content,
            content_type,
            time.time(),
            title,
            is_alert,
            session_id,
            json.dumps(page_actions) if page_actions else None,
            common_url
        ))
        
        PG_CONN.commit()
        return True

    except Exception as e:
        print(f"Error storing in vector DB: {e}")
        PG_CONN.rollback()
        return False

def append_page_actions(url, actions_obj):
    """
    Append one or more actions to the page's JSONB 'page_actions' column.
    """
    global PG_CONN, PG_CURSOR
    if not PG_CONN or not PG_CURSOR:
        if not connect_to_db():
            return False
    try:
        # Ensure row exists
        PG_CURSOR.execute("SELECT 1 FROM page_embeddings WHERE url=%s", (url,))
        exists = PG_CURSOR.fetchone() is not None
        if not exists:
            PG_CURSOR.execute("""
                INSERT INTO page_embeddings (url, content, content_type, timestamp, page_actions, common_url)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (url) DO NOTHING
            """, (url, "", "html", time.time(), json.dumps({"actions": []}), _common_url(url)))

        to_append = actions_obj.get("actions", [])
        if not isinstance(to_append, list) or not to_append:
            return True

        PG_CURSOR.execute("""
            UPDATE page_embeddings
            SET page_actions = jsonb_build_object(
                'actions',
                COALESCE(page_actions->'actions', '[]'::jsonb) || %s::jsonb
            )
            WHERE url = %s
        """, (json.dumps(to_append), url))
        PG_CONN.commit()
        return True
    except Exception as e:
        print(f"Error appending page actions: {e}")
        PG_CONN.rollback()
        return False

def get_page_content(url):
    global PG_CONN, PG_CURSOR
    if not PG_CONN or not PG_CURSOR:
        if not connect_to_db():
            return None
    try:
        PG_CURSOR.execute("""
        SELECT content, timestamp, content_type, title, is_alert, session_id, page_actions, common_url
        FROM page_embeddings
        WHERE url = %s
        """, (url,))
        result = PG_CURSOR.fetchone()
        if result:
            content, timestamp, content_type, title, is_alert, session_id, page_actions, common_url = result
            readable_time = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S") if timestamp else "Unknown"
            return {
                "content": content,
                "timestamp": timestamp,
                "datetime": readable_time,
                "content_type": content_type,
                "title": title,
                "is_alert": is_alert,
                "session_id": session_id,
                "page_actions": page_actions or {"actions": []},
                "common_url": common_url
            }
        return None
    except Exception as e:
        print(f"Error retrieving page content: {e}")
        return None

def get_pages_by_common_url(common_url: str, limit=20):
    global PG_CONN, PG_CURSOR
    if not PG_CONN or not PG_CURSOR:
        if not connect_to_db():
            return []
    try:
        PG_CURSOR.execute("""
        SELECT url, title, page_actions, session_id, timestamp
        FROM page_embeddings
        WHERE common_url = %s
        ORDER BY timestamp DESC
        LIMIT %s
        """, (common_url, limit))
        rows = PG_CURSOR.fetchall()
        return [
            {
                "url": r[0],
                "title": r[1],
                "page_actions": r[2],
                "session_id": r[3],
                "timestamp": r[4]
            }
            for r in rows
        ]
    except Exception as e:
        print(f"Error getting pages by common_url: {e}")
        return []

def find_similar_pages(url, limit=5, session_id=None):
    global PG_CONN, PG_CURSOR
    if not PG_CONN or not PG_CURSOR:
        if not connect_to_db():
            return []
    try:
        PG_CURSOR.execute("""
        SELECT embedding FROM page_embeddings
        WHERE url = %s
        """, (url,))
        result = PG_CURSOR.fetchone()
        if not result:
            return []
        target_embedding = result[0]
        if session_id:
            PG_CURSOR.execute("""
            SELECT url, title, 1 - (embedding <=> %s) as similarity
            FROM page_embeddings
            WHERE url != %s AND session_id = %s
            ORDER BY similarity DESC
            LIMIT %s
            """, (target_embedding, url, session_id, limit))
        else:
            PG_CURSOR.execute("""
            SELECT url, title, 1 - (embedding <=> %s) as similarity
            FROM page_embeddings
            WHERE url != %s
            ORDER BY similarity DESC
            LIMIT %s
            """, (target_embedding, url, limit))
        similar_pages = []
        for similar_url, title, similarity in PG_CURSOR.fetchall():
            similar_pages.append({
                "url": similar_url,
                "title": title or similar_url,
                "similarity": round(similarity * 100, 2)
            })
        return similar_pages
    except Exception as e:
        print(f"Error finding similar pages: {e}")
        return []

def search_pages(query, limit=10, session_id=None):
    global PG_CONN, PG_CURSOR
    if not PG_CONN or not PG_CURSOR:
        if not connect_to_db():
            return []
    try:
        query_embedding = model.encode(query).astype('float32')
        if session_id:
            PG_CURSOR.execute("""
            SELECT url, title, 1 - (embedding <=> %s) as similarity
            FROM page_embeddings
            WHERE session_id = %s
            ORDER BY similarity DESC
            LIMIT %s
            """, (query_embedding.tolist(), session_id, limit))
        else:
            PG_CURSOR.execute("""
            SELECT url, title, 1 - (embedding <=> %s) as similarity
            FROM page_embeddings
            ORDER BY similarity DESC
            LIMIT %s
            """, (query_embedding.tolist(), limit))
        results = []
        for url, title, similarity in PG_CURSOR.fetchall():
            if similarity > 0.5:
                results.append({
                    "url": url,
                    "title": title or url,
                    "similarity": round(similarity * 100, 2)
                })
        return results
    except Exception as e:
        print(f"Error searching pages: {e}")
        return []

def get_all_pages(limit=100, offset=0, session_id=None):
    global PG_CONN, PG_CURSOR
    if not PG_CONN or not PG_CURSOR:
        if not connect_to_db():
            return []
    try:
        if session_id:
            PG_CURSOR.execute("""
            SELECT url, title, content_type, timestamp, is_alert, session_id
            FROM page_embeddings
            WHERE session_id = %s
            ORDER BY timestamp DESC
            LIMIT %s OFFSET %s
            """, (session_id, limit, offset))
        else:
            PG_CURSOR.execute("""
            SELECT url, title, content_type, timestamp, is_alert, session_id
            FROM page_embeddings
            ORDER BY timestamp DESC
            LIMIT %s OFFSET %s
            """, (limit, offset))
        pages = []
        for url, title, content_type, timestamp, is_alert, session_id in PG_CURSOR.fetchall():
            readable_time = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S") if timestamp else "Unknown"
            pages.append({
                "url": url,
                "title": title or url,
                "content_type": content_type,
                "timestamp": timestamp,
                "datetime": readable_time,
                "is_alert": is_alert,
                "session_id": session_id
            })
        return pages
    except Exception as e:
        print(f"Error getting all pages: {e}")
        return []

def get_db_stats(session_id=None):
    global PG_CONN, PG_CURSOR
    if not PG_CONN or not PG_CURSOR:
        if not connect_to_db():
            return {}
    try:
        if session_id:
            PG_CURSOR.execute("SELECT COUNT(*) FROM page_embeddings WHERE session_id = %s", (session_id,))
        else:
            PG_CURSOR.execute("SELECT COUNT(*) FROM page_embeddings")
        total_count = PG_CURSOR.fetchone()[0]
        if session_id:
            PG_CURSOR.execute("""
            SELECT content_type, COUNT(*) 
            FROM page_embeddings 
            WHERE session_id = %s
            GROUP BY content_type
            """, (session_id,))
        else:
            PG_CURSOR.execute("""
            SELECT content_type, COUNT(*) 
            FROM page_embeddings 
            GROUP BY content_type
            """)
        content_types = {row[0]: row[1] for row in PG_CURSOR.fetchall()}
        if session_id:
            PG_CURSOR.execute("""
            SELECT COUNT(*) FROM page_embeddings 
            WHERE is_alert = true AND session_id = %s
            """, (session_id,))
        else:
            PG_CURSOR.execute("SELECT COUNT(*) FROM page_embeddings WHERE is_alert = true")
        alert_count = PG_CURSOR.fetchone()[0]
        if session_id:
            PG_CURSOR.execute("""
            SELECT MIN(timestamp), MAX(timestamp) 
            FROM page_embeddings
            WHERE session_id = %s
            """, (session_id,))
        else:
            PG_CURSOR.execute("SELECT MIN(timestamp), MAX(timestamp) FROM page_embeddings")
        min_ts, max_ts = PG_CURSOR.fetchone()
        return {
            "total": total_count,
            "content_types": content_types,
            "alerts": alert_count,
            "first_capture": datetime.fromtimestamp(min_ts).strftime("%Y-%m-%d %H:%M:%S") if min_ts else None,
            "latest_capture": datetime.fromtimestamp(max_ts).strftime("%Y-%m-%d %H:%M:%S") if max_ts else None
        }
    except Exception as e:
        print(f"Error getting DB stats: {e}")
        return {"error": str(e)}

def close_pg_connection():
    global PG_CONN, PG_CURSOR
    if PG_CURSOR:
        PG_CURSOR.close()
    if PG_CONN:
        PG_CONN.close()

connect_to_db()
init_database()