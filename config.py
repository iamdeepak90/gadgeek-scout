"""Configuration for Gadgeek Tech News Automation

All runtime settings are stored in Redis (shared across all services).
Only connection details for Redis are defined here.
"""

# Redis connection (shared settings store across all services)
REDIS_HOST = "redis"  # Change to your Redis service name in Coolify
REDIS_PORT = 6379
REDIS_DB = 0
REDIS_PASSWORD = "1lDv4AfM8MqY7ZBj8RkdrNlIjfg7P8n73umHI0FKz06cvFawaJrXbFWZIh883qxT"