# Local BGE-M3 embedding service

Run this in a dedicated PowerShell window before running GraphRAG indexing:

```powershell
Set-Location D:\graphrag-main-private-main-source
.\embedding_service\start_embedding_service.ps1
```

The first launch downloads `BAAI/bge-m3` to `D:\graphrag-main-private-main-source\models\bge-m3`. It resumes automatically after an interruption. Keep this window running. Once it prints that Uvicorn is running, the health endpoint is available at `http://127.0.0.1:8001/health`.

GraphRAG uses the OpenAI-compatible endpoint `http://127.0.0.1:8001/v1` configured in `settings.yaml`. No cloud embedding key is needed.
