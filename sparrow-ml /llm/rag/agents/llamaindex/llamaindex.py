from rag.agents.interface import Pipeline
from llama_index.core import VectorStoreIndex, Settings
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.llms.ollama import Ollama
from llama_index.vector_stores.weaviate import WeaviateVectorStore
import weaviate
from pydantic.v1 import create_model
from typing import List
import box
import yaml
from rich.progress import Progress, SpinnerColumn, TextColumn
import warnings
import timeit
import time
import json
from rich import print
from typing import Any


warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning)


class LlamaIndexPipeline(Pipeline):
    def run_pipeline(self,
                     payload: str,
                     query_inputs: [str],
                     query_types: [str],
                     query: str,
                     file_path: str,
                     index_name: str,
                     options: List[str] = None,
                     debug: bool = False,
                     local: bool = True) -> Any:
        print(f"\nRunning pipeline with {payload}\n")

        if len(query_inputs) == 1:
            raise ValueError("Please provide more than one query input")

        start = timeit.default_timer()

        rag_chain = self.build_rag_pipeline(query_inputs, query_types, index_name, debug, local)

        end = timeit.default_timer()
        print(f"Time to prepare RAG pipeline: {end - start}")

        answer = self.process_query(query, rag_chain, debug, local)
        return answer


    def build_rag_pipeline(self, query_inputs, query_types, index_name, debug, local):
        # Import config vars
        with open('config.yml', 'r', encoding='utf8') as ymlfile:
            cfg = box.Box(yaml.safe_load(ymlfile))

        client = self.invoke_pipeline_step(lambda: weaviate.Client(cfg.WEAVIATE_URL),
                                           "Connecting to Weaviate...",
                                           local)

        llm = self.invoke_pipeline_step(lambda: Ollama(model=cfg.LLM, base_url=cfg.OLLAMA_BASE_URL, temperature=0,
                                                       request_timeout=900),
                                        "Loading Ollama...",
                                        local)

        embeddings = self.invoke_pipeline_step(lambda: self.load_embedding_model(model_name=cfg.EMBEDDINGS),
                                               "Loading embedding model...",
                                               local)

        index = self.invoke_pipeline_step(
            lambda: self.build_index(cfg.CHUNK_SIZE, llm, embeddings, client, index_name),
            "Building index...",
            local)

        ResponseModel = self.invoke_pipeline_step(lambda: self.build_response_class(query_inputs, query_types),
                                                  "Building dynamic response class...",
                                                  local)

        # may want to try with similarity_top_k=5, default is 2
        query_engine = self.invoke_pipeline_step(lambda: index.as_query_engine(
                                                            streaming=False,
                                                            output_cls=ResponseModel,
                                                            response_mode="compact"
                                                        ),
                                                 "Constructing query engine...",
                                                 local)

        return query_engine

    # Function to safely evaluate type strings
    def safe_eval_type(self, type_str, context):
        try:
            return eval(type_str, {}, context)
        except NameError:
            raise ValueError(f"Type '{type_str}' is not recognized")

    def build_response_class(self, query_inputs, query_types_as_strings):
        # Controlled context for eval
        context = {
            'List': List,
            'str': str,
            'int': int,
            'float': float
            # Include other necessary types or typing constructs here
        }

        # Convert string representations to actual types
        query_types = [self.safe_eval_type(type_str, context) for type_str in query_types_as_strings]

        # Create fields dictionary
        fields = {name: (type_, ...) for name, type_ in zip(query_inputs, query_types)}

        DynamicModel = create_model('DynamicModel', **fields)

        return DynamicModel

    def load_embedding_model(self, model_name):
        return HuggingFaceEmbedding(model_name=model_name)

    def build_index(self, chunk_size, llm, embed_model, weaviate_client, index_name):
        Settings.chunk_size = chunk_size
        Settings.llm = llm
        Settings.embed_model = embed_model

        vector_store = WeaviateVectorStore(weaviate_client=weaviate_client, index_name=index_name)

        index = VectorStoreIndex.from_vector_store(
            vector_store=vector_store
        )

        return index

    def process_query(self, query, rag_chain, debug=False, local=True):
        start = timeit.default_timer()

        step = 0
        answer = False
        while not answer:
            step += 1
            if step > 1:
                print('Refining answer...')
                # add wait time, before refining to avoid spamming the server
                time.sleep(5)
            if step > 3:
                # if we have refined 3 times, and still no answer, break
                answer = 'No answer found.'
                break

            if local:
                with Progress(
                        SpinnerColumn(),
                        TextColumn("[progress.description]{task.description}"),
                        transient=False,
                ) as progress:
                    progress.add_task(description="Retrieving answer...", total=None)
                    answer = self.get_rag_response(query, rag_chain, debug)
            else:
                print('Retrieving answer...')
                answer = self.get_rag_response(query, rag_chain, debug)

        end = timeit.default_timer()

        print(f"\nJSON response:\n")
        print(answer + '\n')
        print('=' * 50)

        print(f"Time to retrieve answer: {end - start}")

        return answer

    def get_rag_response(self, query, chain, debug=False):
        result = chain.query(query)

        try:
            # Convert and pretty print
            data = json.loads(str(result))
            data = json.dumps(data, indent=4)
            return data
        except (json.decoder.JSONDecodeError, TypeError):
            print("The response is not in JSON format:\n")
            print(result)

        return False

    def invoke_pipeline_step(self, task_call, task_description, local):
        if local:
            with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    transient=False,
            ) as progress:
                progress.add_task(description=task_description, total=None)
                ret = task_call()
        else:
            print(task_description)
            ret = task_call()

        return ret
