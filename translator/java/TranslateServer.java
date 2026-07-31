import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;

import mongodb.jdbc.MongoConnection;
import mongodb.jdbc.MongoStatement;
import unity.annotation.GlobalSchema;
import unity.jdbc.UnityDriver;
import unity.operators.Operator;
import unity.query.GlobalQuery;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;

import java.sql.DriverManager;

/**
 * Example usage:
 *
 * Start the server from the translator/java directory:
 *
 *   java -cp ".;mongodb_unityjdbc_full.jar" TranslateServer
 *
 * Then test a benchmark query:
 *
 *   http://localhost:8082/translate?db=wine_1&sql=SELECT%20T1.Color%20FROM%20grapes%20AS%20T1%20JOIN%20wine%20AS%20T2%20ON%20T1.Grape%20%3D%20T2.Grape%20GROUP%20BY%20T2.Grape%20ORDER%20BY%20AVG%28Price%29%20DESC%20LIMIT%201%3B
 */
public class TranslateServer 
{   
    private static final int DEFAULT_PORT = 8082;
    private static final String DEFAULT_SCHEMA_DIR = "schemas";    
    private static final boolean NORMALIZE_PLAIN_COUNT_ALIAS_TO_COUNT = true;

    public static class Translator 
    {
        private static ConcurrentHashMap <String, MongoConnection> connections = new ConcurrentHashMap<>();

        /**
         * Translates a SQL query to MongoDB (if possible).
         * 
         * @param sql
         *            SQL query to translate
         * @param databaseName
         *            Name of the MongoDB database         
         */
        public static String translate(String sql, String databaseName)                
        {
            try 
            {                                
                // Lookup or create a connection for the given database
                MongoConnection connection = connections.get(databaseName);
                
                if (connection == null) 
                {
                    System.out.println("Creating new connection for database: " + databaseName);
                        String url = "jdbc:mongo://localhost/"+databaseName+"?schema=" + DEFAULT_SCHEMA_DIR + "/mongo_" + databaseName + ".xml&debug=false";
                    connection = (MongoConnection) DriverManager.getConnection(url);
                    connections.put(databaseName, connection);
                }
                
                try (MongoStatement stmt = (MongoStatement) connection.createStatement())
                {
                    GlobalSchema schema = connection.getGlobalSchema();
                    GlobalQuery gq = stmt.translateQuery(sql, false, schema, NORMALIZE_PLAIN_COUNT_ALIAS_TO_COUNT);

                    System.out.println("\n\nTranslating SQL query: \n" + sql + '\n');
                    String mongoQuery = stmt.getQueryString();

                    if (mongoQuery.isEmpty())
                    {
                        System.out.println("SQL query cannot be translated to MongoDB. Here is UnityJDBC logical query tree:");
                        gq.printTree();
                        System.out.println("\nExecution plan:");
                        Operator.printTree(gq.getExecutionTree(), 1);
                    }
                    else
                    {
                        System.out.println("Mongo query: \n" + mongoQuery);
                    }

                    return mongoQuery;
                }                
            } 
            catch (Exception e) 
            {
                // Catch all unexpected exceptions (NPE, ClassCast, etc.) so the server stays alive and returns an empty string instead of crashing.
                System.err.println("Translation error for SQL: " + sql);
                System.err.println("Exception: " + e);
                return "";
            }
        }
    }

    public static void main(String[] args) throws IOException 
    {
        HttpServer server = HttpServer.create(new InetSocketAddress(DEFAULT_PORT), 0);
        server.createContext("/translate", new TranslateHandler());
        server.setExecutor(Executors.newFixedThreadPool(4));

        System.out.println("Version: " + UnityDriver.getVersion());
        System.out.println("Java working directory: " + System.getProperty("user.dir"));
        System.out.println("Listening on http://localhost:8082/translate?db=...&sql=...");
        server.start();
    }

    static class TranslateHandler implements HttpHandler 
    {
        @Override
        public void handle(HttpExchange exchange) throws IOException 
        {
            if (!"GET".equalsIgnoreCase(exchange.getRequestMethod())) 
            {
                Map<String, String> errorMap = new HashMap<>();
                errorMap.put("error", "Only GET is supported");
                sendJson(exchange, 405, errorMap);
                return;
            }

            Map<String, String> params = queryToMap(exchange.getRequestURI().getRawQuery());
            String db = params.get("db");
            String sql = params.get("sql");

            if (db == null || sql == null) 
            {
                Map<String, String> errorMap = new HashMap<>();
                errorMap.put("error", "Missing required parameters 'db' and 'sql'");
                sendJson(exchange, 400, errorMap);
                return;
            }

            String mongo = "Error";
            
            try 
            {
                mongo = Translator.translate(sql, db);
            } 
            catch (Exception e) 
            {
                Map<String, String> errorMap = new HashMap<>();
                errorMap.put("db", db);
                errorMap.put("sql", sql);
                errorMap.put("error", e.toString());
                sendJson(exchange, 500, errorMap);
                return;
            }
            
            Map<String, String> resultMap = new HashMap<>();
            resultMap.put("db", db);
            resultMap.put("sql", sql);
            resultMap.put("version", UnityDriver.getVersion());
            resultMap.put("mongo", mongo+";");
            sendJson(exchange, 200, resultMap);
        }

        private static void sendJson(HttpExchange exchange, int status, Object body) throws IOException 
        {
            String json = toJson(body);
            byte[] bytes = json.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
            exchange.sendResponseHeaders(status, bytes.length);
            try (OutputStream os = exchange.getResponseBody()) {
                os.write(bytes);
            }
        }
        
        private static String toJson(Object body) 
        {
            if (body instanceof Map) 
            {
                Map<?, ?> map = (Map<?, ?>) body;
                StringBuilder sb = new StringBuilder("{");
                boolean first = true;
                for (Map.Entry<?, ?> entry : map.entrySet()) 
                {
                    if (!first) sb.append(",");
                    sb.append("\"").append(entry.getKey()).append("\":\"")
                    .append(entry.getValue().toString().replace("\\", "\\\\").replace("\"", "\\\""))
                    .append("\"");
                    first = false;
                }
                sb.append("}");
                return sb.toString();
            }
            return "{}";
        }

        private static Map<String, String> queryToMap(String query) 
        {
            Map<String, String> result = new HashMap<>();
            if (query == null || query.isEmpty()) return result;
            for (String param : query.split("&")) 
            {
                String[] pair = param.split("=", 2);
                if (pair.length > 0) {
                    String key = urlDecode(pair[0]);
                    String value = pair.length > 1 ? urlDecode(pair[1]) : "";
                    result.put(key, value);
                }
            }
            return result;
        }

        private static String urlDecode(String s) 
        {
            try 
            {
                return URLDecoder.decode(s, StandardCharsets.UTF_8.name());
            } 
            catch (java.io.UnsupportedEncodingException e) 
            {
                throw new RuntimeException("UTF-8 is not supported", e);
            }
        }
    }
}