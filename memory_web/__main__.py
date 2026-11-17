import uvicorn

if __name__ == "__main__":
    # Explicit origin validation still controls HTTP access. No trusted proxy IP headers.
    uvicorn.run(
        "memory_web.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=8080,
        proxy_headers=False,
    )
