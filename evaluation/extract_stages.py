import re
from typing import List, Dict, Any

def _extract_regex_operators(query_str: str) -> bool:
    """Checks if the query string contains regular expression (regex) operator"""
    # checks for explicit $regex operators
    if '$regex' in query_str:
        return True
    # Checks for a regular pattern of the form /pattern/i
    if re.search(r'/[^/]+/[i]?', query_str):
        return True
    return False

def _extract_expr_operators(match_str: str) -> List[str]:
    """Extract the operator from $expr"""
    operators = []
    if '$expr' in match_str:
        operators.append('expr')
        # Extract comparison operators
        for op in ['$eq', '$gt', '$gte', '$lt', '$lte', '$ne', '$not']:
            if op in match_str:
                operators.append(op.lstrip('$'))
    return operators

def _parse_pipeline_stage(stage_str: str) -> List[str]:
    """Parsing a single pipeline stage"""
    operators = []
    
    # Extract the main operators
    stage_ops = re.findall(r'\$(\w+):', stage_str)
    if stage_ops:
        main_op = stage_ops[0]
        operators.append(main_op)
        
        # Processing match phase
        if main_op == 'match':
            # Check the $not operator
            if '$not' in stage_str:
                operators.append('not')
            # Only check regular operation when there is no $not operator
            elif _extract_regex_operators(stage_str):
                operators.append('regex')
            # check expression operators
            operators.extend(_extract_expr_operators(stage_str))
            
        # Handling sub-pipelines in the lookup stage
        elif main_op == 'lookup' and 'pipeline:' in stage_str:
            # Extract the sub-pipeline
            pipeline_match = re.search(r'pipeline:\s*\[(.*?)\]', stage_str, re.DOTALL)
            if pipeline_match:
                sub_pipeline = pipeline_match.group(1)
                # Parse each stage in the sub-pipeline
                sub_stages = re.findall(r'\{(.*?)\}', sub_pipeline, re.DOTALL)
                for sub_stage in sub_stages:
                    operators.extend(_parse_pipeline_stage(sub_stage))
    
    return operators

def get_query_stages(query: str) -> List[str]:
    """
    Extract all operation stages from a MongoDB query statement.
    
    Args:
        query (str): MongoDB query statement
        
    Returns:
        List[str]: List of all operation stages in the query
    """
    stages = []
    
    # cleaning query strings
    query = query.strip()
    
    if ".find(" in query:
        # Processing find queries
        try:
            # Extracting find() parameters
            find_match = re.search(r'\.find\s*\((.*?)\)(?:\s*\.|\s*;|\s*$)', query, re.DOTALL)
            if find_match:
                find_args = find_match.group(1)
                
                # Splitting query conditions and projections
                args = re.findall(r'\{(.*?)\}', find_args, re.DOTALL)
                
                # Processing query conditions
                if args and args[0].strip():
                    stages.append("match")
                    # check regular expressions
                    if _extract_regex_operators(args[0]):
                        stages.append("regex")
                    # check the $not operator
                    if '$not' in args[0]:
                        stages.append("not")
                
                # processing projections
                if len(args) > 1 and args[1].strip():
                    stages.append("project")
                
                # check sorting
                if ".sort(" in query:
                    stages.append("sort")
                    
                # check restrictions
                if ".limit(" in query:
                    stages.append("limit")
                    
        except Exception as e:
            print(f"Error parsing find query: {e}")
            
    elif ".aggregate(" in query:
        # processing aggregate queries
        try:
            # extracting the aggregrate() pipeline
            agg_match = re.search(r'\.aggregate\s*\((.*?)\)(?:\s*;|\s*$)', query, re.DOTALL)
            if agg_match:
                pipeline_str = agg_match.group(1)
                
                # extract each stage in the pipeline
                stages_matches = re.findall(r'\{(.*?)\}', pipeline_str, re.DOTALL)
                
                # analyze each stage
                for stage_str in stages_matches:
                    stages.extend(_parse_pipeline_stage(stage_str))
                    
        except Exception as e:
            print(f"Error parsing aggregate query: {e}")
    
    return stages

if __name__ == "__main__":
    query_string1 = """db.cinema.find(
        { "year": { "$gte": 2000 } },
        { "Name": 1, "Openning_year": 1, "Capacity": 1, "_id": 0 }
    ).sort({"year": -1}).limit(10);"""

    query_string2 = """db.school.aggregate([
      {
        $lookup: {
          from: "driver",
          localField: "School_ID",
          foreignField: "school_bus.School_ID",
          as: "Docs1"
        }
      },
      {
        $unwind: "$Docs1"
      },
      {
        $project: {
          "School": 1,
          "Name": "$Docs1.Name",
          "_id": 0
        }
      }
    ]);"""
    
    query_string3 = """db.departments.aggregate([
    {
        $unwind: "$employees"
    },
    {
        $lookup: {
        from: "regions",
        let: { location_id: "$LOCATION_ID" },
        pipeline: [
            { $unwind: "$countries" },
            { $unwind: "$countries.locations" },
            {
            $match: {
                $expr: {
                $eq: ["$countries.locations.LOCATION_ID", "$$location_id"]
                }
            }
            },
            {
            $project: {
                COUNTRY_NAME: "$countries.COUNTRY_NAME"
            }
            }
        ],
        as: "Docs1"
        }
    },
    {
        $unwind: "$Docs1"
    },
    {
        $project: {
        FIRST_NAME: "$employees.FIRST_NAME",
        LAST_NAME: "$employees.LAST_NAME",
        EMPLOYEE_ID: "$employees.EMPLOYEE_ID",
        COUNTRY_NAME: "$Docs1.COUNTRY_NAME",
        _id: 0
        }
    }
    ]);"""

    query_string4 = """db.Staff.find(
      {
        email_address: { $regex: "wrau", $options: "i" }
      },
      {
        last_name: 1,
        _id: 0
      }
    );"""

    query_string5 = """db.jobs.aggregate([\n  {\n    $unwind: \"$employees\"\n  },\n  {\n    $match: {\n      \"employees.FIRST_NAME\": { $not: /M/i }\n    }\n  },\n  {\n    $project: {\n      EMPLOYEE_ID: \"$employees.EMPLOYEE_ID\",\n      FIRST_NAME: \"$employees.FIRST_NAME\",\n      LAST_NAME: \"$employees.LAST_NAME\",\n      HIRE_DATE: \"$employees.HIRE_DATE\",\n      SALARY: \"$employees.SALARY\",\n      DEPARTMENT_ID: \"$employees.DEPARTMENT_ID\",\n      _id: 0\n    }\n  }\n]);"""

    print("\n" + "="*80 + "\nTest Case 1:")
    stages1 = get_query_stages(query_string1)
    print("Query:\n", query_string1)
    print("Extracted Stages: ", stages1)
    print("Target Stages: ", '["match", "project", "sort", "limit"]')

    print("\n" + "="*80 + "\nTest Case 2:")
    stages2 = get_query_stages(query_string2)
    print("Query:\n", query_string2)
    print("Extracted Stages: ", stages2)
    print("Target Stages: ", '["lookup", "unwind", "project"]')

    print("\n" + "="*80 + "\nTest Case 3:")
    stages3 = get_query_stages(query_string3)
    print("Query:\n", query_string3)
    print("Extracted Stages: ", stages3)
    print("Target Stages: ", '["unwind", "lookup", "unwind", "unwind", "match", "expr", "eq", "project", "unwind", "project"]')

    print("\n" + "="*80 + "\nTest Case 4:")
    stages4 = get_query_stages(query_string4)
    print("Query:\n", query_string4)
    print("Extracted Stages: ", stages4)
    print("Target Stages: ", '["match", "regex", "project"]')

    print("\n" + "="*80 + "\nTest Case 5:")
    stages5 = get_query_stages(query_string5)
    print("Query:\n", query_string5)
    print("Extracted Stages: ", stages5)
    print("Target Stages: ", '["unwind", "match", "not", "project"]')