import json
import re

# Input and output file paths
input_file = "./test.json"       
output_file = "DAILready.json"

tokenizer = re.compile(r"\w+|[^\w\s]")

# Read input
with open(input_file, "r", encoding="utf-8") as f:
    records = json.load(f)

DAILready = []

# Process each entry
for record in records:
    db_id = record["db_id"]
    for query in record["nl_queries"]:
        tokens = tokenizer.findall(query)
        DAILready.append({
            "db_id": db_id,
            "question": query,
            "question_toks": tokens,
            "query": record["ref_sql"]
        })

# Here’s what it’s doing step-by-step:

# tokenizer = re.compile(r"\w+|[^\w\s]")
# Splits text into either:
# Word tokens (letters/numbers) → \w+
# Single punctuation tokens (comma, period, etc.) → [^\w\s]
# This ensures "buses." becomes ["buses", "."].
# Loop over records in test.json
# Takes each nl_queries entry for a given db_id.
# Tokenizes the query into question_toks.
# Appends {db_id, question, question_toks} to the output list.
# Writes DAILready.json
# Saves all flattened and tokenized question records in a single JSON array.

# Write output
with open(output_file, "w", encoding="utf-8") as f:
    json.dump(DAILready, f, indent=2)

print(f"✅ Saved {len(DAILready)} tokenized questions to: {output_file}")