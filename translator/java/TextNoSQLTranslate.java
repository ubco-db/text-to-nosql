import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.List;
import java.util.ArrayList;

import org.bson.BsonValue;
import org.bson.Document;

import com.mongodb.DB;
import com.mongodb.MongoClient;
import com.mongodb.client.MongoCollection;
import com.mongodb.client.MongoDatabase;
import com.mongodb.client.FindIterable;

import mongodb.jdbc.MongoConnection;
import mongodb.jdbc.MongoStatement;
import mongodb.query.MongoQuery;
import unity.annotation.GlobalSchema;
import unity.jdbc.UnityDriver;
import unity.operators.Operator;
import unity.query.GlobalQuery;

public class TextNoSQLTranslate {     
    private static String url;    

    // Mongo JDBC connection
    private static MongoConnection con = null;

    private static boolean normalizePlainCountAliasToCount = true;

    private static final String DEFAULT_SCHEMA_DIR = "translator/java/schemas";

    public static void main(String[] args) {
        try
        {            
            // Configure database
            String dbName = "wine_1"; // TODO: Change to your database name                                                                            

            // Connection URL
            url = "jdbc:mongo://localhost/"+dbName+"?rebuildSchema=false&schema=" + DEFAULT_SCHEMA_DIR + "/mongo_" + dbName + ".xml&debug=false";

            // Test SQL
            String sql;
  
           sql = "SELECT MIN(T1.Color) AS Color\n" + //           
                    "FROM grapes AS T1\n" + //
                    "JOIN wine AS T2 ON T1.Grape = T2.Grape\n" + //
                    "GROUP BY T2.Grape\n" + //
                    "ORDER BY AVG(T2.Price) DESC\n" + //
                    "LIMIT 1;";           
                                
            // System.out.println("Version: " + UnityDriver.getVersion());
            // Make connection. TODO: Change user id and password as needed            
            System.out.println("\nGetting connection:  " + url);
            // con = (MongoConnection) DriverManager.getConnection(url, "admin", "ubco25");
            con = (MongoConnection) DriverManager.getConnection(url);
            System.out.println("\nConnection successful for " + url);

            // Translate SQL to MongoDB query
            System.out.println("\nTranslating SQL to MongoDB query...");
            System.out.println(sql+"\n");
            MongoStatement stmt = translate(sql, con);

            // Execute the translated query
            System.out.println("\nExecuting translated MongoDB query...");
            execute(sql, con, stmt);

            stmt.close();
        }
        catch (SQLException ex)
        {
            System.out.println("Exception: " + ex);
        }
        finally
        {
            if (con != null)
            {
                try
                {	// Close the connection
                    con.close();
                    System.out.println("Connection closed.");
                }
                catch (SQLException ex)
                {
                    System.out.println("SQLException: " + ex);
                }
            }
        }
        System.exit(1);
    }

    
    public static void execute(String sql, MongoConnection connection, MongoStatement stmt)            
    {        
        MongoQuery mq = stmt.getQuery();
        String mongoQuery = stmt.getQueryString();

        System.out.println("\nExecuting SQL query directed on MongoDB: \n" + sql + '\n' + "\nMongo Query: \n"+mongoQuery+"\n");                

        // Check if the query translation was successful
        if (mq == null) {
            System.out.println("ERROR: MongoQuery is null - SQL query could not be translated to MongoDB query.");
            System.out.println("This usually means the SQL query is not supported or there was an error in translation.");
            return;
        }
        
        if (mq.collectionName == null || mq.collectionName.isEmpty()) {
            System.out.println("ERROR: Collection name is null or empty in the translated query.");
            return;
        }

        DB db = connection.getDB();
        MongoClient mongoClient = db.getMongoClient();
        MongoDatabase database = mongoClient.getDatabase(db.getName());        
        MongoCollection<Document> collection = database.getCollection(mq.collectionName);

        // Get just query part (find or aggregate)       
        boolean isAggregate = mongoQuery.startsWith("db."+mq.collectionName+".aggregate(");
        boolean isDistinct = mongoQuery.startsWith("db." + mq.collectionName + ".distinct(");
        boolean isFind = mongoQuery.startsWith("db." + mq.collectionName + ".find(");

        long startTime = System.currentTimeMillis();  

        Iterable<Document> docs = null;
        if (isAggregate) 
        {            
            int start = mongoQuery.indexOf('[');
            int end = mongoQuery.lastIndexOf(']');
            String jsonArray = mongoQuery.substring(start, end + 1); // keep the [ and ]
            Document wrapper = Document.parse("{\"pipeline\": " + jsonArray + "}");            
            List<Document> pipeline = (List<Document>) wrapper.get("pipeline");            
            docs = collection.aggregate(pipeline);
            System.out.println("Executing aggregation pipeline: \n" + pipeline);
        }
        else
        {            

            if (isDistinct)
            {
                int distinctPos = mongoQuery.indexOf(".distinct(");
                int distinctOpen = mongoQuery.indexOf('(', distinctPos);
                int distinctClose =
                        findMatchingParen(mongoQuery, distinctOpen);

                if (distinctOpen < 0 || distinctClose <= distinctOpen)
                {
                    System.out.println("ERROR: Unable to parse distinct() arguments.");
                    return;
                }

                String argumentText = mongoQuery.substring(distinctOpen + 1, distinctClose).trim();

                Document argumentWrapper = Document.parse("{\"arguments\": [" + argumentText + "]}");

                List<?> arguments = (List<?>) argumentWrapper.get("arguments");
                                        
                if (arguments == null || arguments.isEmpty() || !(arguments.get(0) instanceof String))
                {
                    System.out.println("ERROR: distinct() requires a field-name string.");
                    return;
                }

                String fieldName = (String) arguments.get(0);
                Document filter = new Document();

                if (arguments.size() > 1)
                {
                    if (!(arguments.get(1) instanceof Document))
                    {
                        System.out.println("ERROR: distinct() filter must be a document.");
                        return;
                    }

                    filter = (Document) arguments.get(1);
                }

                /*
                * distinct() returns scalar BSON values rather than documents.
                * Wrap each value using the selected field name so the existing
                * result-printing code can continue using Iterable<Document>.
                */
                List<Document> distinctDocuments = new ArrayList<Document>();

                for (BsonValue value : collection.distinct(fieldName, filter, BsonValue.class))
                {
                    distinctDocuments.add(new Document(fieldName, value));
                }

                docs = distinctDocuments;
            }
            else if (isFind)
            {
                int findPos = mongoQuery.indexOf(".find(");
                int findOpen = mongoQuery.indexOf('(', findPos);
                int findClose = findMatchingParen(mongoQuery, findOpen);

                String findArgsText = mongoQuery.substring(findOpen + 1, findClose);

                // Wrapping the arguments in an array safely handles both:
                // find(filter)
                // find(filter, projection)
                Document argsWrapper = Document.parse("{\"args\": [" + findArgsText + "]}");

                @SuppressWarnings("unchecked")
                List<Document> findArgs = (List<Document>) argsWrapper.get("args");

                Document filter = findArgs.isEmpty() ? new Document() : findArgs.get(0);

                FindIterable<Document> cursor = collection.find(filter);

                if (findArgs.size() > 1) {
                    cursor = cursor.projection(findArgs.get(1));
                }

                // Process chained cursor operations after find(...).
                String tail = mongoQuery.substring(findClose + 1).trim();

                while (!tail.isEmpty() && !tail.equals(";")) {
                    if (tail.startsWith(".sort(")) {
                        int open = tail.indexOf('(');
                        int close = findMatchingParen(tail, open);

                        Document sort = Document.parse(tail.substring(open + 1, close)
                        );

                        cursor = cursor.sort(sort);
                        tail = tail.substring(close + 1).trim();
                    }
                    else if (tail.startsWith(".limit(")) {
                        int open = tail.indexOf('(');
                        int close = findMatchingParen(tail, open);

                        int limit = Integer.parseInt(tail.substring(open + 1, close).trim()
                        );

                        cursor = cursor.limit(limit);
                        tail = tail.substring(close + 1).trim();
                    }
                    else if (tail.startsWith(".skip(")) {
                        int open = tail.indexOf('(');
                        int close = findMatchingParen(tail, open);

                        int skip = Integer.parseInt(tail.substring(open + 1, close).trim()
                        );

                        cursor = cursor.skip(skip);
                        tail = tail.substring(close + 1).trim();
                    }
                    else {
                        throw new IllegalArgumentException("Unsupported find cursor operation: " + tail);
                    }
                }

                docs = cursor;
            }
            else
            {
                System.out.println("ERROR: Unsupported MongoDB query method.");
                return;
            }
        }
                    
        System.out.println("Results");
        int count = 0;
        for (Document doc : docs) {            
            // doc.toString();
            System.out.println(doc.toJson());
            count++;
        }                           
        System.out.println("Time for direct mongo query (in ms): "+(System.currentTimeMillis()-startTime));
        System.out.println("Total results: "+count);            
    }

    private static int findMatchingParen(String text, int openPos)
    {
        int depth = 0;
        boolean inString = false;
        boolean escaped = false;
        char quote = 0;

        for (int i = openPos; i < text.length(); i++)
        {
            char c = text.charAt(i);

            if (inString)
            {
                if (escaped)
                {
                    escaped = false;
                }
                else if (c == '\\')
                {
                    escaped = true;
                }
                else if (c == quote)
                {
                    inString = false;
                }

                continue;
            }

            if (c == '"' || c == '\'')
            {
                inString = true;
                quote = c;
            }
            else if (c == '(')
            {
                depth++;
            }
            else if (c == ')' && --depth == 0)
            {
                return i;
            }
        }

        throw new IllegalArgumentException("Unmatched parenthesis in Mongo query: " + text);
    }

    /**
     * Translates a SQL query to MongoDB (if possible).
     * 
     * @param sql
     *            SQL query to translate
     * @param connection
     *            MongoConnection or null if translating without a connection
     * @throws SQLException
     *             if a database or translation error occurs
     */
    public static MongoStatement translate(String sql, MongoConnection connection)
            throws SQLException
    {
        MongoStatement stmt;
        GlobalQuery gq;
        GlobalSchema schema = null;
        
        if (connection != null)
        {   // Translate using a connection
            schema = connection.getGlobalSchema();
            stmt = (MongoStatement) connection.createStatement();
        }
        else
        {   // Translate without a connection
            stmt = new MongoStatement();
        }

        boolean schemaValidation = true;
        gq = stmt.translateQuery(sql, schemaValidation, schema, normalizePlainCountAliasToCount);
        gq.printTree();

        System.out.println("\n\nTranslating SQL query: \n" + sql + '\n');
        String mongoQuery = stmt.getQueryString();
        if (mongoQuery.equals(""))
        {    // Query could not be executed by Mongo, output Unity execution plan
            System.out.println("SQL query cannot be directly translated.  Here is logical query tree: ");
            gq.printTree();
            System.out.println("\nExecution plan: ");
            Operator.printTree(gq.getExecutionTree(), 1);
        }
        else
        {
            System.out.println("To Mongo query: \n" + mongoQuery);
        }       

        return stmt;
    }
}