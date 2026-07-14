import re

class MongoFieldParser:
    def __init__(self):
        self.fields = set()
        
    def _parse_mongodb_query(self, query_str: str) -> dict:
        """Parsing MongoDB query strings using regular expressions"""
        try:
            # List of MongoDB operators
            operators = {
                # Comparison operators
                "eq", "gt", "gte", "in", "lt", "lte", "ne", "nin",
                # Logical operators
                "and", "not", "nor", "or",
                # Element wise operators
                "exists", "type",
                # Evaluation operators
                "expr", "jsonSchema", "mod", "regex", "text", "where",
                # Geospatial operators
                "geoIntersects", "geoWithin", "near", "nearSphere",
                # Array operators
                "all", "elemMatch", "size",
                # bitwise operators
                "bitsAllClear", "bitsAllSet", "bitsAnyClear", "bitsAnySet",
                # aggregation operators
                "sum", "avg", "first", "last", "max", "min", "push", "addToSet",
                # aggregration phase operators
                "group", "match", "project", "limit", "skip", "sort", "unwind",
                # aggregration phase parameters
                "from", "localField", "foreignField", "as", "pipeline",
                # count and control fields
                "count", "size",
                # other operators
                "comment", "rand"
            }
            
            # Extract field names (quoted and unquoted)
            fields = set()
            
            # Matches quoted field names
            quoted_fields = re.finditer(r'"([^"$][^"]*)":', query_str)
            for match in quoted_fields:
                field = match.group(1)
                if field not in operators and not field.startswith("$"):
                    self._add_normalized_field(fields, field)
            
            # Matches unquoted field names
            unquoted_fields = re.finditer(r'([a-zA-Z_$][\w$]*)\s*:', query_str)
            for match in unquoted_fields:
                field = match.group(1)
                if not field.startswith("$") and field not in operators:
                    self._add_normalized_field(fields, field)
            
            # Matching field references（$field）
            field_refs = re.finditer(r'\$([a-zA-Z_$][\w$]*(?:\.[a-zA-Z_$][\w$]*)*)', query_str)
            for match in field_refs:
                field = match.group(1)
                if not any(part in operators for part in field.split('.')):
                    self._add_normalized_field(fields, field)
            
            return {"fields": sorted(list(fields))}
        except Exception as e:
            print(f"Error extracting fields: {e}")
            return None
            
    def _add_normalized_field(self, fields: set, field: str) -> None:
        """add normalized field name to the set"""
        # split field path
        parts = field.split('.')
        
        # If the field is nested under 'employees' or 'employee', take the last part
        if any(part in ["employees", "employee"] for part in parts[:-1]):
            field = parts[-1]
        
        # add field if it's not purely numeric and not a common nested object name
        if not field.isdigit() and field not in ["employees", "employee", "job_history"]:
            fields.add(field)

    def _parse_aggregate_query(self, query: str) -> None:
        """resolving fields in aggregate queries"""
        try:
            # extract the parameter part in aggregate()
            match = re.search(r'\.aggregate\s*\((.*)\)\s*;?\s*$', query, re.DOTALL | re.MULTILINE)
            if match:
                pipeline_str = match.group(1).strip()
                
                # Extract all stages
                stages = re.finditer(r'\{[^{}]*\}', pipeline_str)
                for stage_match in stages:
                    stage_str = stage_match.group(0)
                    stage = self._parse_mongodb_query(stage_str)
                    if isinstance(stage, dict):
                        if "fields" in stage:
                            for field in stage["fields"]:
                                self.fields.add(field)
                        
                        # Check if its the $count stage
                        count_match = re.search(r'"?\$count"?\s*:\s*"([^"]+)"', stage_str)
                        if count_match:
                            self.fields.add(count_match.group(1))
        except Exception as e:
            print(f"Error parsing aggregate query: {e}")

    def _parse_find_query(self, query: str) -> None:
        """parsing fields in find queries"""
        try:
            #extract the parameter part in find() 
            match = re.search(r'\.find\s*\((.*)\)\s*;?\s*$', query, re.DOTALL | re.MULTILINE)
            if match:
                args_str = match.group(1).strip()
                
                # extract all parameters
                args_matches = re.finditer(r'\{[^{}]*\}', args_str)
                args = []
                for arg_match in args_matches:
                    arg = self._parse_mongodb_query(arg_match.group(0))
                    if isinstance(arg, dict) and "fields" in arg:
                        args.append(arg["fields"])
                
                # processing query conditions
                if len(args) > 0:
                    for field in args[0]:
                        self.fields.add(field)
                
                # processing projections
                if len(args) > 1:
                    for field in args[1]:
                        self.fields.add(field)
        except Exception as e:
            print(f"Error parsing find query: {e}")

    def parse_query(self, query: str) -> list:
        """
        Parse a MongoDB query statement to extract all used fields.
        
        Args:
            query (str): MongoDB query statement
            
        Returns:
            List[str]: List of all fields used in the query
        """
        self.fields.clear()
        
        try:
            # cleaning up the query string
            query = query.strip()
            
            if ".find(" in query:
                self._parse_find_query(query)
            elif ".aggregate(" in query:
                self._parse_aggregate_query(query)
                
            return sorted(list(self.fields))
        except Exception as e:
            print(f"Error parsing query: {e}")
            return []

def extract_fields(MQL: str) -> list:
    """
    Extract all fields used in a MongoDB query statement.
    
    Args:
        MQL (str): MongoDB query statement
        
    Returns:
        List[str]: list of all fields used in the query
    """
    parser = MongoFieldParser()
    return parser.parse_query(MQL)

if __name__ == "__main__":

    test_queries = [
        "db.musical.aggregate([\n  {\n    $group: {\n      _id: \"$Result\",\n      count: { $sum: 1 }\n    }\n  },\n  {\n    $sort: { count: -1 }\n  },\n  {\n    $limit: 1\n  },\n  {\n    $project: {\n      _id: 0,\n      Result: \"$_id\"\n    }\n  }\n]);"
    ]

    parser = MongoFieldParser()
    for i, query in enumerate(test_queries, 1):
        print(f"\nTest Query {i}:")
        print(query)
        fields = parser.parse_query(query)
        print("Extracted fields:", fields)