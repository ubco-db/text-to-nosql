import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.List;

import org.bson.Document;

import com.mongodb.DB;
import com.mongodb.MongoClient;
import com.mongodb.client.MongoCollection;
import com.mongodb.client.MongoDatabase;

import mongodb.jdbc.MongoConnection;
import mongodb.jdbc.MongoStatement;
import mongodb.query.MongoQuery;
import unity.annotation.GlobalSchema;
import unity.jdbc.UnityDriver;
import unity.operators.Operator;
import unity.query.GlobalQuery;

/**
 * TextNoSQLTranslate allows testing individual SQL queries against a MongoDB database.
 * Requires mongodb_unityjdbc_full.jar in the CLASS_PATH.
 */
public class TextNoSQLTranslate 
{            
    // Mongo JDBC connection
    private static MongoConnection con = null;

    private static final String DEFAULT_SCHEMA_DIR = "translator/java/schemas";

    private static final boolean NORMALIZE_PLAIN_COUNT_ALIAS_TO_COUNT = true;

    public static void main(String[] args) 
    {
        try
        {            
            // Configure database
            String dbName = "wine_1"; 

            // Connection URL
            String url = "jdbc:mongo://localhost/"+dbName+"?rebuildSchema=false&schema=" + DEFAULT_SCHEMA_DIR + "/mongo_" + dbName + ".xml&debug=false";

            // Test SQL
            String sql;           
            sql = "SELECT T1.Color FROM grapes AS T1 JOIN wine AS T2 ON T1.Grape = T2.Grape GROUP BY T2.Grape ORDER BY AVG(Price) DESC LIMIT 1;"; 

            System.out.println("Version: " + UnityDriver.getVersion());

            // Make connection. TODO: Change user id and password as needed            
            System.out.println("\nGetting connection:  " + url);
            con = (MongoConnection) DriverManager.getConnection(url);
            
            System.out.println("\nConnection successful for " + url);

            // Translate SQL to MongoDB query
            System.out.println("\nTranslating SQL to MongoDB query...");            

            // Execute the translated query
            try (MongoStatement stmt = translate(sql, con))
            {
                System.out.println("\nExecuting translated MongoDB query...");
                execute(sql, con, stmt);
            }
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
    }

    /**
     * Executes the translated MongoDB query directly using the MongoDB Java driver.
     * This returns JSON-style MongoDB documents rather than relational JDBC rows.
     * 
     * @param sql
     *            SQL query corresponding to MongoDB query
     * @param connection
     *            MongoConnection
     * @param stmt
     *           MongoStatement containing the translated MongoDB query     
     */
    @SuppressWarnings("unchecked")
    public static void execute(String sql, MongoConnection connection, MongoStatement stmt)          
    {        
        MongoQuery mq = stmt.getQuery();
        String mongoQuery = stmt.getQueryString();

        System.out.println("\nExecuting SQL query directly on MongoDB: \n" + sql + '\n' + "\nMongo Query: \n"+mongoQuery+"\n");                

        DB db = connection.getDB();
        MongoClient mongoClient = db.getMongoClient();
        MongoDatabase database = mongoClient.getDatabase(db.getName());        
        MongoCollection<Document> collection = database.getCollection(mq.collectionName);

        // Get just query part (find or aggregate)       
        boolean isAggregate = mongoQuery.startsWith("db."+mq.collectionName+".aggregate(");

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
            int start = mongoQuery.indexOf('(');
            int end = mongoQuery.lastIndexOf(')');
            String queryStr = mongoQuery.substring(start + 1, end);             
            
            // Split into filter and projection parts
            String[] parts = queryStr.split("},\\s*\\{", 2);
            Document filter = Document.parse(parts[0] + "}");

            // Only if projection exists            
            if (parts.length > 1) {
                Document projection = Document.parse("{" + parts[1]);
                docs = collection.find(filter).projection(projection);
            } else {
                docs = collection.find(filter);
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

        boolean schemaValidation = false;                // This will allow a query to be executed without a schema (or a Mongo connection)
        gq = stmt.translateQuery(sql, schemaValidation, schema, NORMALIZE_PLAIN_COUNT_ALIAS_TO_COUNT);

        System.out.println("\n\nTranslating SQL query: \n" + sql + '\n');
        String mongoQuery = stmt.getQueryString();
        if (mongoQuery.isEmpty())
        {    // Query could not be executed by Mongo, output Unity execution plan
            System.out.println("SQL query cannot be directly executed by UnityJDBC.  Here is UnityJDBC logical query tree: ");
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