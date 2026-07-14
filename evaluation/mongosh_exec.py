import subprocess
import json
import os
from typing import Union, List, Dict
from datetime import datetime
from tqdm import tqdm
import shutil
import platform
import re

class MongoShellExecutor:
    def __init__(self, connection_string: str = "mongodb://localhost:27017", output_dir: str = "output", mongosh_path: str = None):
        self.connection_string = connection_string
        self.mongosh_path = mongosh_path or self._get_mongosh_path()
        self.output_dir = output_dir
        
        # Create output directory if it doesn't exist
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # Test the connection upon initialization
        self._test_connection()

    def _get_mongosh_path(self) -> str:
        """Find a mongosh/mongo executable across platforms."""
        # 1) Env var override
        env_path = os.environ.get("MONGOSH_PATH")
        if env_path and os.path.exists(env_path):
            print(f"Using mongosh from MONGOSH_PATH: {env_path}")
            return env_path

        # 2) PATH lookup
        for binary in ("mongosh", "mongo"):
            p = shutil.which(binary)
            if p:
                print(f"Using MongoDB shell from PATH: {p}")
                return p

        # 3) Common macOS Homebrew paths
        mac_paths = [
            "/opt/homebrew/bin/mongosh",   # Apple Silicon
            "/usr/local/bin/mongosh",      # Intel
            "/opt/homebrew/bin/mongo",
            "/usr/local/bin/mongo",
        ]
        for p in mac_paths:
            if os.path.exists(p):
                print(f"Found MongoDB shell at: {p}")
                return p

        # 4) Windows fallbacks (your originals)
        win_paths = [
            r"C:\Program Files\MongoDB\Server\7.0\bin\mongosh.exe",
            r"C:\Program Files\MongoDB\Server\6.0\bin\mongosh.exe",
            r"C:\Program Files\MongoDB\Server\5.0\bin\mongosh.exe",
            r"C:\Program Files\MongoDB\Server\4.4\bin\mongo.exe",
            r"C:\Program Files\MongoDB\Server\4.2\bin\mongo.exe",
            r"D:\MongoDB\Server\7.0\bin\mongosh.exe",
            r"D:\MongoDB\Server\6.0\bin\mongosh.exe",
            r"D:\MongoDB\Server\5.0\bin\mongosh.exe",
            r"D:\MongoDB\Server\4.4\bin\mongo.exe",
            r"D:\MongoDB\Server\4.2\bin\mongo.exe",
        ]
        for p in win_paths:
            if os.path.exists(p):
                print(f"Found MongoDB shell at: {p}")
                return p

        raise FileNotFoundError(
            "Could not find mongosh or mongo executable. "
            "Install mongosh or set MONGOSH_PATH or pass mongosh_path=..."
        )
    
    def _format_query(self, query: str) -> str:
        """Formatting query strings"""
        q = query.strip().rstrip(';')
        
        if ('.find(' in q or '.aggregate(' in q) and '.toArray()' not in q:
            q += '.toArray()'
            
        return q

    def _save_to_json(self, data: Union[List, Dict], filename: str = None) -> str:
        """Save the results to a JSON file"""
        if filename is None:
            # Generate filenames using timestamps
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"query_result_{timestamp}.json"
        
        filepath = os.path.join(self.output_dir, filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            
        return filepath

    def _startupinfo(self):
        """Windows-only STARTUPINFO to hide the console window."""
        if platform.system().lower().startswith("win"):
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            return si
        return None
    # Previous code for execute_query is commented out
    # def execute_query(
    #     self,
    #     db_name: str,
    #     query: str,
    #     output_file: str = None,
    #     timeout: int = 30,
    #     get_str: bool = False
    # ) -> List[Dict]:
    #     try:
    #         formatted_query = self._format_query(query)
    #         js_command = f'''
    #             try {{
    #                 db = connect("{self.connection_string}").getSiblingDB("{db_name}");
    #                 result = {formatted_query};
    #                 printjson(result);
    #             }} catch (e) {{
    #                 print("QUERY_ERROR: " + e.message);
    #                 quit(1);
    #             }}
    #         '''

    #         result = subprocess.run(
    #             [self.mongosh_path, "--quiet", "--eval", js_command],
    #             capture_output=True,
    #             text=True,
    #             timeout=timeout,
    #             encoding='utf-8',
    #             errors='replace',
    #             startupinfo=self._startupinfo()
    #         )

    #         if result.stderr:
    #             print(f"Query Error: {result.stderr}")
    #             return []

    #         output = result.stdout.strip()
    #         if not output:
    #             print("The query result is empty")
    #             return []

    #         if "QUERY_ERROR" in output:
    #             print(f"Query Execution error: {output}")
    #             return []

    #         try:
    #             # Normalize special Mongo types so json.loads can parse
    #             special_types = [
    #                 (r'ObjectId\(([^)]+)\)', r'"ObjectId(\1)"'),
    #                 (r'ISODate\(([^)]+)\)', r'"ISODate(\1)"'),
    #                 (r'NumberLong\(([^)]+)\)', r'\1'),
    #                 (r'NumberDecimal\(([^)]+)\)', r'\1'),
    #                 (r'Timestamp\(([^)]+)\)', r'"Timestamp(\1)"'),
    #                 (r'BinData\(([^)]+)\)', r'"BinData(\1)"'),
    #                 (r'DBRef\(([^)]+)\)', r'"DBRef(\1)"'),
    #                 (r'NumberInt\(([^)]+)\)', r'\1'),
    #                 (r'Date\(([^)]+)\)', r'"Date(\1)"'),
    #             ]
    #             for pattern, repl in special_types:
    #                 output = re.sub(pattern, repl, output)

    #             data = json.loads(output)
    #             if not isinstance(data, list):
    #                 data = [data]
    #             for item in data:
    #                 if not isinstance(item, (dict, list)):
    #                     print(f"Warning: unexpected data item type - {type(item)}")
    #             return data

    #         except json.JSONDecodeError as e:
    #             if get_str:
    #                 return f"Error in Executing Query and Transforming result into JSON: `{str(e)}`"
    #             else:
    #                 print(f"JSON parsing error: {str(e)}")
    #                 print(f"Error position: {e.pos}")
    #                 print(f"Error line: {e.lineno}, Column: {e.colno}")
    #                 print(f"Original output fragment: {output[max(0, e.pos-50):e.pos+50]}")
    #             return []

    #         except Exception as e:
    #             if get_str:
    #                 return f"Error in Executing Query and Transforming result into JSON: `{str(e)}`"
    #             else:
    #                 print(f"Data conversion error: {str(e)}")
    #                 print(f"Raw output: {output[:200]}...")
    #             return []

    #     except subprocess.TimeoutExpired:
    #         if get_str:
    #             return f"Query Timeout: `{timeout} seconds`"
    #         else:
    #             print(f"Query timeout (>{timeout} seconds)")
    #         return []
    #     except Exception as e:
    #         if get_str:
    #             return f"Error in Executing Query and Transforming result into JSON: `{str(e)}`"
    #         else:
    #             print(f"An error occurred while executing the query: {str(e)}")
    #         return []

    def execute_query(
        self,
        db_name: str,
        query: str,
        output_file: str = None,
        timeout: int = 30,
        get_str: bool = False
    ):
        """
        Execute a MongoDB query via mongosh.

        - Ensures .toArray() for find/aggregate so results are materialized.
        - Prints strict JSON using EJSON.stringify(..., {relaxed:true}).
        - If get_str == False: returns Python object (list/dict) parsed from JSON.
          If get_str == True:  returns raw JSON string (stdout) as-is.
        """
        try:
            # 1) Normalize/complete query
            q = self._format_query(query)  # adds .toArray() if needed and strips trailing ';'

            # 2) Build the JS we ask mongosh to run
            js_command = f'''
                try {{
                    db = connect("{self.connection_string}").getSiblingDB("{db_name}");
                    const __result = {q};
                    // Force strict JSON (relaxed:true keeps numbers/strings human-friendly)
                    const __out = EJSON.stringify(__result, {{relaxed: true}});
                    print(__out);
                }} catch (e) {{
                    // Machine-detectable error marker; avoid noisy stacks
                    print("__QUERY_ERROR__:" + e.message);
                    quit(1);
                }}
            '''

            # 3) Windows-only STARTUPINFO to hide window; None elsewhere
            startupinfo = self._startupinfo()

            # 4) Run mongosh
            result = subprocess.run(
                [self.mongosh_path, "--quiet", "--eval", js_command],
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding='utf-8',
                errors='replace',
                startupinfo=startupinfo
            )

            # 5) Handle stderr (shell errors) quickly
            if result.stderr:
                return result.stderr if get_str else []

            stdout = result.stdout.strip()

            # 6) Bail out on explicit error marker
            if stdout.startswith("__QUERY_ERROR__:"):
                return stdout if get_str else []

            # 7) Respect get_str contract
            if get_str:
                # At this point stdout is valid JSON (EJSON.stringify)
                return stdout

            # 8) Parse JSON into Python types
            if not stdout:
                return []
            data = json.loads(stdout)
            # Normalize to list for downstream code
            return data if isinstance(data, list) else [data]

        except subprocess.TimeoutExpired:
            return (f"__QUERY_ERROR__:timeout>{timeout}s") if get_str else []
        except Exception as e:
            # On unexpected issues, either return [] or a tagged string
            return (f"__QUERY_ERROR__:{type(e).__name__}:{e}") if get_str else []


    def execute_script(self, db_name: str, script_path: str) -> str:
        try:
            command = [self.mongosh_path, db_name, script_path]
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            print(f"Error while executing script: {e.stderr}")
            raise

    def _test_connection(self):
        """Quick shell availability & connection test."""
        try:
            print(f"Trying to connect to MongoDB: {self.connection_string}")
            js_command = f'''
                try {{
                    db = connect("{self.connection_string}");
                    print("CONNECTION_SUCCESS");
                }} catch (e) {{
                    print("CONNECTION_ERROR: " + e.message);
                    quit(1);
                }}
            '''
            result = subprocess.run(
                [self.mongosh_path, "--quiet", "--eval", js_command],
                capture_output=True,
                text=True,
                timeout=5,
                encoding='utf-8',
                errors='replace',
                startupinfo=self._startupinfo()
            )
            if result.stderr:
                raise Exception(f"MongoDB connection error: {result.stderr}")
            if "CONNECTION_SUCCESS" not in (result.stdout or ""):
                raise Exception("MongoDB connection failed")
            print("MongoDB connection test successful!")
        except Exception as e:
            print(f"MongoDB connection test failed: {str(e)}")
            print("\nPlease make sure:")
            print("1. mongosh is installed and on PATH, or pass mongosh_path / set MONGOSH_PATH")
            print("2. MongoDB service is running on the target host/port")
            print(f"3. Using shell at: {self.mongosh_path}")
            raise