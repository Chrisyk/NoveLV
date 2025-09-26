#!/usr/bin/env python3
"""
Run the Anki Data Extractor Flask application
"""
from flaskr import create_app

if __name__ == '__main__':
    app = create_app()
    print("Starting Anki Data Extractor Flask App...")
    print("Open your browser and go to: http://localhost:5000")
    print("Make sure Anki is running with AnkiConnect addon installed!")
    app.run(debug=True, host='0.0.0.0', port=5000)