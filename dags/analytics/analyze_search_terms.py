from airflow.decorators import dag, task
from airflow.datasets import Dataset
from airflow.providers.weaviate.hooks.weaviate import WeaviateHook
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
from airflow.models.baseoperator import chain
from airflow.exceptions import AirflowSkipException
from pendulum import datetime
from airflow.providers.snowflake.hooks.snowflake import SnowflakeHook
import os
import json

_WEAVIATE_CONN_ID = os.getenv("WEAVIATE_CONN_ID")
_WEAVIATE_COLLECTION_NAME = "Analytics"

_SNOWLFLAKE_CONN_ID = os.getenv("SNOWFLAKE_CONN_ID", "snowflake_default")
_SNOWFLAKE_DB_NAME = "hybrid_search_demo"
_SNOWFLAKE_SCHEMA_NAME = "dev"
_SNOWFLAKE_TABLE_NAME_CATEGORIZATION = "search_terms"
_SNOWFLAKE_TABLE_NAME_INSIGHTS = "search_summary"


@dag(
    start_date=datetime(2024, 7, 1),
    schedule=[Dataset(f"weaviate://{_WEAVIATE_CONN_ID}@{_WEAVIATE_COLLECTION_NAME}/")],
    catchup=False,
    tags=["use-case", "demo", "GenAI"],
)
def analyze_search_terms():

    create_table_if_not_exists_categorization = SQLExecuteQueryOperator(
        task_id="create_table_if_not_exists_categorization",
        conn_id=_SNOWLFLAKE_CONN_ID,
        sql=f"""
                CREATE TABLE IF NOT EXISTS 
                {_SNOWFLAKE_DB_NAME}.{_SNOWFLAKE_SCHEMA_NAME}.{_SNOWFLAKE_TABLE_NAME_CATEGORIZATION} (
                    uuid STRING PRIMARY KEY,
                    term STRING,
                    broadcategory STRING,
                    narrowcategory STRING
                );
            """,
        show_return_value_in_logs=True,
    )

    create_table_if_not_exists_insights = SQLExecuteQueryOperator(
        task_id="create_table_if_not_exists_insights",
        conn_id=_SNOWLFLAKE_CONN_ID,
        sql=f"""
                CREATE TABLE IF NOT EXISTS 
                {_SNOWFLAKE_DB_NAME}.{_SNOWFLAKE_SCHEMA_NAME}.{_SNOWFLAKE_TABLE_NAME_INSIGHTS} (
                    uuid STRING PRIMARY KEY,
                    insight VARCHAR(16777216)
                );
            """,
        show_return_value_in_logs=True,
    )

    @task
    def pull_history_from_weaviate():
        hook = WeaviateHook(_WEAVIATE_CONN_ID)

        search_term_collection = hook.get_collection(_WEAVIATE_COLLECTION_NAME)
        all_search_terms = [
            search.properties["searchterm"]
            for search in search_term_collection.iterator(
                return_properties=["searchterm"]
            )
        ]

        chunk_size = 50
        split_search_terms = [
            all_search_terms[i : i + chunk_size]
            for i in range(0, len(all_search_terms), chunk_size)
        ]

        return split_search_terms

    @task
    def categorize_search_terms(list_of_search_terms):
        from openai import OpenAI

        input_prompt = "\n".join(
            [f"{{'term{n}': '{term}'}}" for n, term in enumerate(list_of_search_terms)]
        )

        system_prompt = """
        Please categorize the following list of search terms into appropriate 
        narrow and broad categories and provide the categories and a uuid4 in 
        a json format with one dictionary for each term, collected in a list:

        Items with the same narrow category always have the same broad category.

        Provide the output in the following JSON format:
        [
            {
                "uuid": "uuid1",
                "term": "term1",
                "broadcategory": "broadcategory1",
                "narrowcategory": "narrowcategory1"
            },
            {
                "uuid": "uuid2",
                "term": "term2",
                "broadcategory": "broadcategory2",
                "narrowccategory": "narrowcategory3"
            },
            {
                "uuid": "uuid3",
                "term": "term3",
                "broadcategory": "broadcategory1",
                "narrowcategory": "narrowcategory2"
            },
            ...

        ]
        """

        user_prompt = f"""
            Search terms:
            {input_prompt}
        """

        client = OpenAI()

        chat_completion = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )

        categorized_searches = chat_completion.choices[0].message.content

        return json.loads(categorized_searches)

    search_history = pull_history_from_weaviate()

    categorized_searches = categorize_search_terms.expand(
        list_of_search_terms=search_history
    )

    @task(map_index_template="{{ my_custom_map_index }}")
    def insert_data_into_snowflake(input_data):

        required_keys = {"uuid", "term", "broadcategory", "narrowcategory"}

        if not all(key in input_data for key in required_keys):
            missing_keys = required_keys - input_data.keys()
            raise AirflowSkipException(
                f"Skipping for malformed Input: Missing keys: {missing_keys}"
            )

        unexpected_keys = input_data.keys() - required_keys
        if unexpected_keys:
            raise AirflowSkipException(
                f"Skipping for malformed Input: Unexpected keys: {unexpected_keys}"
            )

        insert_sql = f"""
        INSERT INTO {_SNOWFLAKE_DB_NAME}.{_SNOWFLAKE_SCHEMA_NAME}.{_SNOWFLAKE_TABLE_NAME_CATEGORIZATION} (
            uuid, term, broadcategory, narrowcategory
        ) VALUES (
            %(uuid)s, %(term)s, %(broadcategory)s, %(narrowcategory)s
        );
        """
        snowflake_hook = SnowflakeHook(snowflake_conn_id=_SNOWLFLAKE_CONN_ID)
        snowflake_hook.run(insert_sql, parameters=input_data)

        # get the current context and define the custom map index variable
        from airflow.operators.python import get_current_context

        context = get_current_context()
        context["my_custom_map_index"] = f"Inserting info on: {input_data['term']}"

    insert_data_into_snowflake_obj = insert_data_into_snowflake.expand(
        input_data=categorized_searches
    )

    @task
    def provide_overall_analysis(list_of_search_terms):
        from openai import OpenAI

        input_prompt = "\n".join([f"- {term}" for term in list_of_search_terms])

        system_prompt = """
        You are an AI assistant that provides insights from a list of product 
        search terms. Analyze the provided search terms and identify frequent 
        themes, trends, and product requests. Provide detailed insights and 
        suggestions based on the search terms.

        Provide the output in the following JSON format:
        
        {
            "uuid": "<UUID generated in the format of df986214-d52b-4842-a07a-29bb2568c111>",
            "insight": "<your generated detailed insights text>"
        }
        
        """

        user_prompt = f"""
            Search terms:
            {input_prompt}
        """

        client = OpenAI()

        chat_completion = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )

        categorized_searches = chat_completion.choices[0].message.content

        return json.loads(categorized_searches)

    provide_overall_analysis_obj = provide_overall_analysis(
        list_of_search_terms=search_history
    )

    @task
    def insert_data_into_snowflake_insights(input_data):



        insert_sql = f"""
        INSERT INTO {_SNOWFLAKE_DB_NAME}.{_SNOWFLAKE_SCHEMA_NAME}.{_SNOWFLAKE_TABLE_NAME_INSIGHTS} (
            uuid, insight
        ) VALUES (
            %(uuid)s, %(insight)s
        );
        """
        snowflake_hook = SnowflakeHook(snowflake_conn_id=_SNOWLFLAKE_CONN_ID)
        snowflake_hook.run(insert_sql, parameters=input_data)

    insert_data_into_snowflake_insights_obj = insert_data_into_snowflake_insights(
        input_data=provide_overall_analysis_obj
    )

    chain(create_table_if_not_exists_categorization, insert_data_into_snowflake_obj)
    chain(create_table_if_not_exists_insights, insert_data_into_snowflake_insights_obj)


analyze_search_terms()
