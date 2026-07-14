import os
import json

# INPUT directory: contains original JSON files, each representing a MongoDB database
input_dir = "./mongodb_data"

# OUTPUT directory: will store flattened collections (1 file per collection)
output_dir = "./flattened_mongodb_collections"
os.makedirs(output_dir, exist_ok=True)  # Create the output directory if it doesn't exist

# Loop over every file in the input directory
for file_name in os.listdir(input_dir):
    if not file_name.endswith(".json"):
        continue  # Skip files that are not .json

    db_name = file_name.replace(".json", "")  # Use the filename (without .json) as the database name
    db_output_dir = os.path.join(output_dir, db_name)  # Create a subdirectory for this database
    os.makedirs(db_output_dir, exist_ok=True)  # Ensure the database output folder exists

    file_path = os.path.join(input_dir, file_name)  # Full path to the input JSON file
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)  # Load the JSON file into a dictionary

    # Loop through each top-level key in the dictionary (each key is a collection name)
    for collection_name, documents in data.items():
        output_path = os.path.join(db_output_dir, f"{collection_name}.json")  # Path to write this collection
        with open(output_path, "w", encoding="utf-8") as out_f:
            json.dump(documents, out_f, indent=2)  # Save the list of documents as a formatted JSON file

# Done!
print("✅ JSON files flattened into collections.")
