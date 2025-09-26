import sqlite3
import json
import hashlib
from datetime import datetime
from typing import Dict, List, Optional, Tuple

DATABASE_PATH = 'data/scan_history.db'

def get_db_connection():
    """Get database connection with row factory"""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_database():
    """Initialize the scan history database with required tables"""
    conn = get_db_connection()
    
    # Create scan_history table to store analysis results
    conn.execute('''
        CREATE TABLE IF NOT EXISTS scan_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text_hash TEXT NOT NULL,
            filename TEXT,
            text_content TEXT,
            text_length INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            
            -- Analysis results
            comprehension_rate REAL,
            difficulty_level TEXT,
            total_words INTEGER,
            total_instances INTEGER,
            total_processed_words INTEGER,
            known_words_count INTEGER,
            unknown_words_count INTEGER,
            ignored_words_count INTEGER,
            
            -- JSON data for detailed results
            known_words_json TEXT,
            unknown_words_json TEXT,
            ignored_words_json TEXT,
            star_distribution_json TEXT
        )
    ''')
    
    # Create index on text_hash for faster lookups
    conn.execute('CREATE INDEX IF NOT EXISTS idx_text_hash ON scan_history(text_hash)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_created_at ON scan_history(created_at)')
    
    # Add total_processed_words column if it doesn't exist (migration for existing databases)
    try:
        conn.execute('ALTER TABLE scan_history ADD COLUMN total_processed_words INTEGER')
        conn.commit()
        print("Added total_processed_words column to existing database")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            # Column already exists, that's fine
            pass
        else:
            print(f"Warning: Could not add total_processed_words column: {e}")
    
    conn.commit()
    conn.close()

def generate_text_hash(text_content: str) -> str:
    """Generate a hash for the text content to identify duplicates"""
    # Normalize text by removing extra whitespace and converting to lowercase
    normalized_text = ' '.join(text_content.lower().split())
    return hashlib.md5(normalized_text.encode('utf-8')).hexdigest()

def save_scan_result(analysis_data: Dict, text_content: str, filename: Optional[str] = None) -> Optional[int]:
    """Save scan result to database. Creates a new entry each time, allowing multiple analyses of the same text. Returns the scan ID."""
    text_hash = generate_text_hash(text_content)
    
    conn = get_db_connection()
    
    try:
        # Insert new scan result (allows multiple analyses of same text)
        cursor = conn.execute('''
            INSERT INTO scan_history (
                text_hash, filename, text_content, text_length,
                comprehension_rate, difficulty_level, total_words, total_instances, total_processed_words,
                known_words_count, unknown_words_count, ignored_words_count,
                known_words_json, unknown_words_json, ignored_words_json, star_distribution_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            text_hash,
            filename,
            text_content,
            len(text_content),
            analysis_data.get('comprehension_rate', 0.0),
            analysis_data.get('difficulty_level', 'Unknown'),
            analysis_data.get('total_words', 0),
            analysis_data.get('total_instances', 0),
            analysis_data.get('total_processed_words', 0),
            len(analysis_data.get('known_words', [])),
            len(analysis_data.get('unknown_words', [])),
            len(analysis_data.get('ignored_words', [])),
            json.dumps(analysis_data.get('known_words', []), ensure_ascii=False),
            json.dumps(analysis_data.get('unknown_words', []), ensure_ascii=False),
            json.dumps(analysis_data.get('ignored_words', []), ensure_ascii=False),
            json.dumps(analysis_data.get('star_distribution', {}), ensure_ascii=False)
        ))
        
        scan_id = cursor.lastrowid
        conn.commit()
        return scan_id if scan_id is not None else 0
        
    finally:
        conn.close()

def get_scan_by_hash(text_hash: str) -> Optional[Dict]:
    """Get most recent scan result by text hash (since multiple analyses can exist for same text)"""
    conn = get_db_connection()
    
    try:
        row = conn.execute(
            'SELECT * FROM scan_history WHERE text_hash = ? ORDER BY created_at DESC LIMIT 1',
            (text_hash,)
        ).fetchone()
        
        if row:
            return dict(row)
        return None
        
    finally:
        conn.close()

def check_if_text_analyzed(text_content: str) -> Optional[Dict]:
    """Check if text has been analyzed before - returns most recent analysis if found"""
    text_hash = generate_text_hash(text_content)
    return get_scan_by_hash(text_hash)

def get_scan_history(limit: int = 50) -> List[Dict]:
    """Get scan history ordered by creation date (newest first)"""
    conn = get_db_connection()
    
    try:
        rows = conn.execute('''
            SELECT id, filename, text_length, created_at, comprehension_rate, 
                   difficulty_level, total_words, known_words_count, 
                   unknown_words_count, ignored_words_count
            FROM scan_history 
            ORDER BY created_at DESC 
            LIMIT ?
        ''', (limit,)).fetchall()
        
        return [dict(row) for row in rows]
        
    finally:
        conn.close()

def get_scan_by_id(scan_id: int) -> Optional[Dict]:
    """Get full scan result by ID"""
    conn = get_db_connection()
    
    try:
        row = conn.execute(
            'SELECT * FROM scan_history WHERE id = ?',
            (scan_id,)
        ).fetchone()
        
        if row:
            scan_data = dict(row)
            # Parse JSON fields back to objects
            scan_data['known_words'] = json.loads(scan_data['known_words_json'] or '[]')
            scan_data['unknown_words'] = json.loads(scan_data['unknown_words_json'] or '[]')
            scan_data['ignored_words'] = json.loads(scan_data['ignored_words_json'] or '[]')
            scan_data['star_distribution'] = json.loads(scan_data['star_distribution_json'] or '{}')
            
            return scan_data
        return None
        
    finally:
        conn.close()

def delete_scan(scan_id: int) -> bool:
    """Delete a scan from history"""
    conn = get_db_connection()
    
    try:
        cursor = conn.execute('DELETE FROM scan_history WHERE id = ?', (scan_id,))
        conn.commit()
        return cursor.rowcount > 0
        
    finally:
        conn.close()

def get_progress_comparison(limit: int = 10) -> List[Dict]:
    """Get recent scans for progress comparison"""
    conn = get_db_connection()
    
    try:
        rows = conn.execute('''
            SELECT id, filename, created_at, comprehension_rate, 
                   difficulty_level, total_words, known_words_count, 
                   unknown_words_count, ignored_words_count
            FROM scan_history 
            ORDER BY created_at DESC 
            LIMIT ?
        ''', (limit,)).fetchall()
        
        return [dict(row) for row in rows]
        
    finally:
        conn.close()

def get_scans_by_filename(filename: str) -> List[Dict]:
    """Get all scans for a specific filename"""
    conn = get_db_connection()
    
    try:
        rows = conn.execute('''
            SELECT id, filename, created_at, comprehension_rate, 
                   difficulty_level, total_words, known_words_count, 
                   unknown_words_count, ignored_words_count, text_length
            FROM scan_history 
            WHERE filename = ?
            ORDER BY created_at DESC
        ''', (filename,)).fetchall()
        
        return [dict(row) for row in rows]
        
    finally:
        conn.close()

# Initialize database when module is imported
init_database()