#!/usr/bin/env python3
"""
Run the Anki Data Extractor Flask application
"""
from flaskr import create_app

if __name__ == '__main__':
    import os
    app = create_app()
    print("Starting Anki Data Extractor Flask App...")
    print("Open your browser and go to: http://localhost:5000")
    print("Make sure Anki is running with AnkiConnect addon installed!")
    debug = bool(app.config.get("DEBUG")) or os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(debug=debug, host='0.0.0.0', port=int(os.environ.get("PORT", "5000")))