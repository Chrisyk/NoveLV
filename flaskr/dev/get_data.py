# Objective: get all the user known words from Anki from their Note Type
import requests
import json
import pickle
import os
from datetime import datetime
from typing import Dict, Any, Optional

class AnkiDataManager:
    def __init__(self, cache_dir: str = "data"):
        self.cache_dir = cache_dir
        self.ensure_cache_dir()
        
    def ensure_cache_dir(self):
        """Create cache directory if it doesn't exist"""
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)
    
    def ankiConnectInvoke(self, action: str, version: int, params: Dict = {}) -> Optional[Dict]:
        """Invoke AnkiConnect API"""
        url = "http://127.0.0.1:8765"
        payload = {'action': action, 'version': version, 'params': params or {}}
        response = requests.post(url, json=payload)

        if response.status_code == 200:
            data = response.json()
            if data.get('error') is None:
                return data
            else:
                print(f"AnkiConnect Error: {data['error']}")
                return None
        else:
            print(f"HTTP Error: {response.status_code}")
            return None

    def get_cache_filename(self, note_type: str, field_name: str) -> tuple:
        """Generate cache filenames for both JSON and pickle formats"""
        safe_note = note_type.replace(" ", "_").replace(":", "_")
        safe_field = field_name.replace(" ", "_")
        base_name = f"{safe_note}_{safe_field}"
        json_file = os.path.join(self.cache_dir, f"{base_name}.json")
        pickle_file = os.path.join(self.cache_dir, f"{base_name}.pkl")
        return json_file, pickle_file

    def load_cache(self, note_type: str, field_name: str) -> Dict[str, Any]:
        """Load cached data with metadata"""
        json_file, pickle_file = self.get_cache_filename(note_type, field_name)
        
        # Try pickle first (faster)
        if os.path.exists(pickle_file):
            try:
                with open(pickle_file, 'rb') as f:
                    return pickle.load(f)
            except:
                pass
        
        # Fallback to JSON
        if os.path.exists(json_file):
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        
        # Return empty structure if no cache
        return {
            'metadata': {
                'note_type': note_type,
                'field_name': field_name,
                'last_updated': None,
                'total_cards': 0
            },
            'cards': {}  # card_id -> {expression, note_id, mod_time, etc.}
        }

    def save_cache(self, data: Dict[str, Any], note_type: str, field_name: str):
        """Save data to both JSON and pickle formats"""
        json_file, pickle_file = self.get_cache_filename(note_type, field_name)
        
        # Update metadata
        data['metadata']['last_updated'] = datetime.now().isoformat()
        data['metadata']['total_cards'] = len(data['cards'])
        
        # Save as JSON (human readable)
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        # Save as pickle (faster loading)
        with open(pickle_file, 'wb') as f:
            pickle.dump(data, f)

    def get_modified_cards(self, note_type: str, last_check_time: Optional[str] = None) -> list:
        """Get cards modified since last check"""
        # If no last check time, get all cards
        if not last_check_time:
            query = f'note:"{note_type}"'
        else:
            # Convert ISO time to Anki's timestamp format (seconds since epoch)
            try:
                dt = datetime.fromisoformat(last_check_time.replace('Z', '+00:00'))
                timestamp = int(dt.timestamp())
                query = f'note:"{note_type}" edited:{timestamp}'
            except:
                query = f'note:"{note_type}"'
        
        card_ids_result = self.ankiConnectInvoke('findCards', 6, {'query': query})
        return card_ids_result['result'] if card_ids_result else []

    def update_card_cache(self, note_type: str, field_name: str, force_full_update: bool = False) -> Dict[str, Any]:
        """Update cache with incremental or full update"""
        print(f"Updating cache for {note_type} - {field_name}...")
        
        # Load existing cache
        cache_data = self.load_cache(note_type, field_name)
        
        # Determine what cards to fetch
        if force_full_update:
            print("Performing full update...")
            card_ids = self.get_modified_cards(note_type)
        else:
            last_updated = cache_data['metadata']['last_updated']
            card_ids = self.get_modified_cards(note_type, last_updated)
            if not card_ids:
                print("No updates needed.")
                return cache_data
            print(f"Found {len(card_ids)} modified cards to update...")

        # Fetch card details
        if card_ids:
            cards_info_result = self.ankiConnectInvoke('cardsInfo', 6, {'cards': card_ids})
            if not cards_info_result:
                return cache_data

            # Update cache with new/modified cards
            for card in cards_info_result['result']:
                card_id = str(card['cardId'])
                if field_name in card['fields']:
                    cache_data['cards'][card_id] = {
                        'expression': card['fields'][field_name]['value'],
                        'note_id': card['note'],
                        'deck_name': card['deckName'],
                        'card_type': card['type'],
                        'modified': card['mod'],
                        'cached_at': datetime.now().isoformat()
                    }

        # Save updated cache
        self.save_cache(cache_data, note_type, field_name)
        print(f"Cache updated! Total cards: {len(cache_data['cards'])}")
        return cache_data

    def get_expressions(self, note_type: str, field_name: str, update_cache: bool = True) -> list:
        """Get all expressions from cache, optionally updating first"""
        if update_cache:
            cache_data = self.update_card_cache(note_type, field_name)
        else:
            cache_data = self.load_cache(note_type, field_name)
        
        expressions = []
        for card_data in cache_data['cards'].values():
            expr = card_data['expression'].strip()
            if expr:  # Only add non-empty expressions
                expressions.append(expr)
        
        return list(set(expressions))  # Remove duplicates

def main():
    """Main function demonstrating the AnkiDataManager usage"""
    manager = AnkiDataManager()
    
    # Get available note types
    noteTypes = manager.ankiConnectInvoke('modelNamesAndIds', 6)
    if not noteTypes:
        print("Failed to get note types from Anki")
        return
    
    print("Available note types:")
    for note in noteTypes['result']:
        print(f"  - {note}")
    
    selectedNote = input("\nSelect a note type: ").strip()
    if selectedNote not in noteTypes['result']:
        print("Note type not found")
        return

    # Get available fields for the selected note type
    fields = manager.ankiConnectInvoke('modelFieldNames', 6, {'modelName': selectedNote})
    if not fields:
        print("Failed to get fields for note type")
        return
    
    print(f"\nAvailable fields for '{selectedNote}':")
    for field in fields['result']:
        print(f"  - {field}")
    
    field = input("\nSelect a field to extract expressions from: ").strip()
    if field not in fields['result']:
        print("Field not found")
        return

    # Check if cache exists and offer options
    cache_data = manager.load_cache(selectedNote, field)
    if cache_data['metadata']['last_updated']:
        print(f"\nFound existing cache from {cache_data['metadata']['last_updated']}")
        print(f"Contains {cache_data['metadata']['total_cards']} cards")
        
        choice = input("Choose: (u)pdate incrementally, (f)ull refresh, or (s)kip update? [u/f/s]: ").lower()
        if choice == 'f':
            expressions = manager.get_expressions(selectedNote, field, update_cache=True)
            cache_data = manager.update_card_cache(selectedNote, field, force_full_update=True)
        elif choice == 's':
            expressions = manager.get_expressions(selectedNote, field, update_cache=False)
        else:  # default to incremental update
            expressions = manager.get_expressions(selectedNote, field, update_cache=True)
    else:
        print("\nNo cache found. Performing initial data fetch...")
        expressions = manager.get_expressions(selectedNote, field, update_cache=True)
    
    # Display results
    print(f"\n=== Results ===")
    print(f"Found {len(expressions)} unique expressions:")
    for i, expr in enumerate(expressions[:10], 1):  # Show first 10
        print(f"{i:2d}. {expr}")
    
    if len(expressions) > 10:
        print(f"... and {len(expressions) - 10} more expressions")
    
    # Show cache location
    json_file, pickle_file = manager.get_cache_filename(selectedNote, field)
    print(f"\nCache saved to:")
    print(f"  JSON: {json_file}")
    print(f"  Pickle: {pickle_file}")

if __name__ == "__main__":
    main()