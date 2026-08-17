import sqlite3
import os
import sys
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server
mcp = FastMCP("Dwarf Fortress Wiki Search")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PROJECT_DIR, "df_wiki.db")
sys.path.insert(0, PROJECT_DIR)
from code import ensure_database_ready

@mcp.tool()
def search_wiki_articles(query: str, namespace: str = None) -> str:
    """
    Search across the Dwarf Fortress wiki articles using full-text search (FTS5).
    Returns highly ranked matching article titles and a brief snippet.
    
    :param query: The search keywords or phrase (e.g., "magma pump", "steel production").
    :param namespace: Optional filter. Use 'Main', 'DF2012', 'Masterwork', or 'Guides'.
    """
    ensure_database_ready()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Base query utilizing SQLite's BM25 relevance ranking algorithm
    if namespace:
        sql = """
            SELECT a.title, a.namespace, snippet(wiki_search_index, 1, '...', '...', '...', 15) as snip
            FROM wiki_search_index f
            JOIN wiki_articles a ON f.rowid = a.id
            WHERE wiki_search_index MATCH ? AND a.namespace = ?
            ORDER BY bm25(wiki_search_index)
            LIMIT 10;
        """
        params = (query, namespace)
    else:
        sql = """
            SELECT a.title, a.namespace, snippet(wiki_search_index, 1, '...', '...', '...', 15) as snip
            FROM wiki_search_index f
            JOIN wiki_articles a ON f.rowid = a.id
            WHERE wiki_search_index MATCH ?
            ORDER BY bm25(wiki_search_index)
            LIMIT 10;
        """
        params = (query,)
        
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            return f"No wiki articles found matching: '{query}'" + (f" in namespace '{namespace}'" if namespace else "")
            
        result = [f"Found {len(rows)} matching articles:\n"]
        for title, ns, snippet_text in rows:
            # Clean up newlines in snippets for cleaner output
            clean_snip = snippet_text.replace("\n", " ").strip()
            result.append(f"- **{title}** [{ns}]\n  *Snippet:* ...{clean_snip}...\n")
            
        return "\n".join(result)
    except sqlite3.OperationalError as e:
        conn.close()
        return f"Search parser error. Try simpler keywords. Error: {e}"

@mcp.tool()
def read_wiki_article(title: str) -> str:
    """
    Retrieve the full, cleaned body text of a specific Dwarf Fortress wiki page.
    Use this once you know the exact title of the article you need.
    
    :param title: The exact title of the article (e.g., "DF2012:Starting build" or "Magma").
    """
    ensure_database_ready()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT title, namespace, body_content 
        FROM wiki_articles 
        WHERE title = ?
    """, (title,))
    
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return f"Error: Article titled '{title}' not found in the database. Use search_wiki_articles to verify the title."
        
    art_title, ns, content = row
    
    output = [
        f"# Title: {art_title}",
        f"**Namespace:** {ns}",
        "---",
        content
    ]
    return "\n\n".join(output)

if __name__ == "__main__":
    # Standard FastMCP entrypoint for running via stdio
    mcp.run()