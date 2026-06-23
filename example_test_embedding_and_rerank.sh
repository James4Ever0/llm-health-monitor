curl http://localhost:4000/v1/rerank \
  -H "Authorization: Bearer sk-my-proxy-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "rerank-english-v3.0",
    "query": "What is the capital of France?",
    "documents": [
      "Paris is the capital of France.",
      "Berlin is the capital of Germany.",
      "France is a country in Europe."
    ]
  }'

curl http://localhost:4000/v1/embeddings \
  -H "Authorization: Bearer sk-my-proxy-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "text-embedding-3-small",
    "input": "Hello world"
  }'


# since embedding and rerank expectation will be different than llm text response, and the params are different.
# for embedding, can we expect embedding dimension?
# for rerank, can we expect rerank entry to be one of the many?
# or simply expect a simple md5 hash of responsed content?
# and create different default params than llm params, set endpoint type (llm, embedding, rerank)
# but the entry name of config param override shall be the same as llm.
