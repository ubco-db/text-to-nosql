"""
MongoDB Field Parser
Used to parse database fields in MongoDB queries.

This module extracts and lists all fields used in MongoDB queries,
including find queries, aggregation pipelines, and complex expressions.
"""

import json
import demjson
from typing import List, Set, Dict, Union, Any

class MongoFieldParser:
    def __init__(self):
        # Fields to store each collection
        self.collection_fields = {}
        self.current_collection = None
        # Storing calculated and temporary fields
        self.computed_fields = set()
        
    def parse_query(self, query: str) -> Dict[str, List[str]]:
        """
        Parse a MongoDB query statement to extract all used fields.
        
        Args:
            query (str): MongoDB query statement
            
        Returns:
            Dict[str, List[str]]: List of fields for each collection
        """
        # Extract the collection name L
        try:
            collection = query.split("db.", 1)[1].split(".", 1)[0]
            self.current_collection = collection
            
            if collection not in self.collection_fields:
                self.collection_fields[collection] = set()
        except Exception as e:
            print(f"Error extracting collection name: {e}")
            return {}
        
        if ".find(" in query:
            self._parse_find_query(query)
        elif ".aggregate(" in query:
            self._parse_aggregate_query(query)
            
        # Returns a sorted list of fields for each collection
        return {coll: sorted(list(fields)) 
                for coll, fields in self.collection_fields.items()}
    
    def _add_field(self, field: str, collection: str = None) -> None:
        """Add a field to the current collection's field set"""
        if collection is None:
            collection = self.current_collection
            
        # Check if the field should be excluded
        if self._should_exclude_field(field):
            return
            
        if collection not in self.collection_fields:
            self.collection_fields[collection] = set()
        self.collection_fields[collection].add(field)
    
    def _should_exclude_field(self, field: str) -> bool:
        """check if a field should be excluded from parsing"""
        # Exclude the following fields：
        # 1. Variable references starting with $$
        # 2. _id subfields (in the group stage)
        # 3. lookup as fields
        # 4. calculated fields (EVc)
        return (field.startswith("$$") or
                field.startswith("_id.") or
                field in self.computed_fields)

    def _add_computed_field(self, field: str) -> None:
        """Add a calculated field"""
        self.computed_fields.add(field)
        
    def _parse_find_query(self, query: str) -> None:
        """Parsing fields in find queries"""
        try:
            # Extracting parameters from find()
            args_str = query.split(".find(", 1)[1].rsplit(")", 1)[0].strip()
            
            # Processing parameter strings
            args_str = args_str.replace("'", '"')  # replace single quotes with double quotes
            args_str = args_str.replace("None", "null")  # handling Python's None
            args_str = args_str.replace("True", "true").replace("False", "false")  # handling boolean values
            
            # make sure args_str is a valid JSON array
            if not args_str.startswith("["):
                args_str = "[" + args_str + "]"
                
            try:
                args = demjson.decode(args_str)
            except:
                # If parsing fails, try splitting arguments
                parts = args_str.split("}, {")
                if len(parts) > 1:
                    # Rebuild parameter strings
                    args_str = "[{"
                    args_str += "}, {".join(parts)
                    args_str += "}]"
                    args = demjson.decode(args_str)
                else:
                    raise
                    
            if args and isinstance(args[0], dict):
                self._extract_fields_from_dict(args[0])
                
            # Processing projection fields (second argument)
            if len(args) > 1 and isinstance(args[1], dict):
                self._extract_projection_fields(args[1])
        except Exception as e:
            print(f"Error parsing find query: {e}")
    
    def _parse_aggregate_query(self, query: str) -> None:
        """resolving fields in aggregate queries"""
        try:
            # Extracting parameters from aggregate()
            pipeline_str = query.split(".aggregate(", 1)[1].rsplit(")", 1)[0].strip()
            
            # Processing parameter strings
            pipeline_str = pipeline_str.replace("'", '"')  # replace single quotes with double quotes
            pipeline_str = pipeline_str.replace("None", "null")  # Handling Python's None
            pipeline_str = pipeline_str.replace("True", "true").replace("False", "false")  # Handling boolean values
            
            # Try parsing directly
            try:
                pipeline = demjson.decode(pipeline_str)
            except:
                # If parsing fails, try to normalize MongoDB operators
                pipeline_str = pipeline_str.replace("$match:", '"$match":')
                pipeline_str = pipeline_str.replace("$group:", '"$group":')
                pipeline_str = pipeline_str.replace("$project:", '"$project":')
                pipeline_str = pipeline_str.replace("$lookup:", '"$lookup":')
                pipeline_str = pipeline_str.replace("$unwind:", '"$unwind":')
                pipeline_str = pipeline_str.replace("$sort:", '"$sort":')
                pipeline_str = pipeline_str.replace("from:", '"from":')
                pipeline_str = pipeline_str.replace("localField:", '"localField":')
                pipeline_str = pipeline_str.replace("foreignField:", '"foreignField":')
                pipeline_str = pipeline_str.replace("as:", '"as":')
                pipeline_str = pipeline_str.replace("pipeline:", '"pipeline":')
                pipeline = demjson.decode(pipeline_str)
                
            if isinstance(pipeline, list):
                for stage in pipeline:
                    if isinstance(stage, dict):
                        self._parse_aggregate_stage(stage)
        except Exception as e:
            print(f"Error parsing aggregate query: {e}")
    
    def _parse_aggregate_stage(self, stage: Dict) -> None:
        """analyze each stage of the aggregation pipeline"""
        if not isinstance(stage, dict):
            return
            
        for operator, value in stage.items():
            if operator == "$match":
                if "$expr" in value:
                    self._handle_expr_operator(value["$expr"])
                else:
                    self._extract_fields_from_dict(value)
            elif operator == "$group":
                self._extract_group_fields(value)
            elif operator == "$project":
                self._extract_projection_fields(value)
            elif operator == "$lookup":
                self._extract_lookup_fields(value)
            elif operator == "$unwind":
                # handling $unwind operations
                if isinstance(value, str):
                    self._add_field(value.strip("$"))
                elif isinstance(value, dict) and "path" in value:
                    self._add_field(value["path"].strip("$"))
            elif operator == "$sort":
                # handling $sort operations
                if isinstance(value, dict):
                    for field in value.keys():
                        if not field.startswith("$"):
                            self._add_field(field)
            elif operator in ["$count", "$limit", "$skip"]:
                pass
    
    def _handle_expr_operator(self, expr_value: dict) -> None:
        """handle $expr operator in MongoDB queries"""
        if isinstance(expr_value, dict):
            for op, value in expr_value.items():
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, str) and item.startswith("$"):
                            if not self._should_exclude_field(item):
                                self._add_field(item.strip("$"))
                        elif isinstance(item, dict):
                            self._extract_fields_from_dict(item)
                elif isinstance(value, str) and value.startswith("$"):
                    if not self._should_exclude_field(value):
                        self._add_field(value.strip("$"))
                elif isinstance(value, dict):
                    self._extract_fields_from_dict(value)
    
    def _extract_fields_from_dict(self, d: Dict, parent: str = "") -> None:
        """recursively extract fields from a dictionary"""
        if not isinstance(d, dict):
            return
            
        for key, value in d.items():
            if key == "$expr":
                self._handle_expr_operator(value)
                continue
            elif key.startswith("$"):
                continue
                
            full_key = f"{parent}.{key}" if parent else key
            
            if isinstance(value, dict):
                if all(k.startswith("$") for k in value.keys()):
                    self._add_field(full_key)
                self._extract_fields_from_dict(value, full_key)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        self._extract_fields_from_dict(item, full_key)
            else:
                self._add_field(full_key)
    
    def _extract_group_fields(self, group_dict: Dict) -> None:
        """Extract fields in $group stage"""
        for key, value in group_dict.items():
            # Mark all fields in $group as computed fields
            if key != "_id":
                self._add_computed_field(key)
                
            if key == "_id":
                if isinstance(value, str) and value.startswith("$"):
                    self._add_field(value[1:])
                elif isinstance(value, dict):
                    for _, field in value.items():
                        if isinstance(field, str) and field.startswith("$"):
                            self._add_field(field[1:])
            elif isinstance(value, dict):
                for op, field in value.items():
                    if isinstance(field, str) and field.startswith("$"):
                        self._add_field(field[1:])
    
    def _extract_projection_fields(self, proj_dict: Dict) -> None:
        """Extracting fields from $project stage"""
        for key, value in proj_dict.items():
            if not key.startswith("$"):
                if isinstance(value, (int, bool)) and value:
                    # Simple field projection
                    self._add_field(key)
                elif isinstance(value, str) and value.startswith("$"):
                    # Field references
                    self._add_field(value[1:])
                elif isinstance(value, dict):
                    # Mark fields that use expressions as calculated fields
                    self._add_computed_field(key)
                    for op, field in value.items():
                        if isinstance(field, str) and field.startswith("$"):
                            self._add_field(field[1:])
                            
    def _extract_lookup_fields(self, lookup_dict: Dict) -> None:
        """Extract fields from $lookup stage"""
        if not isinstance(lookup_dict, dict):
            return
            
        # Record the original collection 
        original_collection = self.current_collection
            
        # Mark the field as calculated field 
        if "as" in lookup_dict:
            self._add_computed_field(lookup_dict["as"])
            
        if "localField" in lookup_dict:
            self._add_field(lookup_dict["localField"])
        if "foreignField" in lookup_dict and "from" in lookup_dict:
            self._add_field(lookup_dict["foreignField"], lookup_dict["from"])
            
        if "pipeline" in lookup_dict and "from" in lookup_dict:
            self.current_collection = lookup_dict["from"]
            pipeline = lookup_dict["pipeline"]
            if isinstance(pipeline, list):
                for stage in pipeline:
                    if isinstance(stage, dict):
                        self._parse_aggregate_stage(stage)
                        
        # Restore the original collection
        self.current_collection = original_collection

def main():
    """test example"""
    parser = MongoFieldParser()
    
    # file_path = "./data/train/TEND_train.json"
    # db_schema_path = "./mongodb_data/{db_id}.json"
    # with open(file_path, "r") as f:
    #     data = json.load(f)
    # record_id = 666
    # sample = [example for example in data if example['record_id'] == record_id][0]
    # db_id = sample['db_id']
    # query = sample['MQL']
    query = "db.Customers.aggregate([\n  {\n    $unwind: \"$Customer_Addresses\"\n  },\n  {\n    $lookup: {\n      from: \"Addresses\",\n      localField: \"Customer_Addresses.address_id\",\n      foreignField: \"address_id\",\n      as: \"Docs1\"\n    }\n  },\n  {\n    $unwind: \"$Docs1\"\n  },\n  {\n    $match: {\n      \"Docs1.city\": \"Lake Geovannyton\"\n    }\n  },\n  {\n    $group: {\n      _id: null,\n      count: { $sum: 1 }\n    }\n  },\n  {\n    $project: {\n      _id: 0,\n      count: 1\n    }\n  }\n]);\n"

    print("Query:", query)
    fields = parser.parse_query(query)
    print("Fields:", json.dumps(fields, indent=4))

if __name__ == "__main__":
    main()
