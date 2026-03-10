"""Configuration for Gadgeek Tech News Automation

All runtime settings are stored in Redis (shared across all services).
Only connection details for Redis are defined here.
"""

# Redis connection (shared settings store across all services)
# From Coolify Redis URL: redis://default:PASSWORD@HOST:6379/0
REDIS_HOST = "xs444swscgwwk48owwgow8oo"  # Internal hostname from Coolify
REDIS_PORT = 6379
REDIS_DB = 0
REDIS_USERNAME = "default"
REDIS_PASSWORD = "OS8kINpfEqkBHyWJUVaQaSyPKiFnZnSXEhOW1CS0Jb3ZIXH4s620Ml0WhefkBqu0"