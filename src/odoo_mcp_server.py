#!/usr/bin/env python3
"""
Odoo 19 MCP Server

This MCP server provides tools to interact with Odoo 19's External JSON-2 API.
Supports multiple company configurations with enhanced error handling, retry logic,
and image processing capabilities.
"""

import os
import json
import logging
import time
import contextvars
from typing import Any, Optional, Dict
from configparser import ConfigParser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from mcp.server import Server
from mcp.types import Tool, TextContent
import mcp.server.stdio
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn

# Configure logging with more detail
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("odoo-mcp-server")

# Configuration file path
CONFIG_FILE = os.getenv("ODOO_CONFIG_FILE", ".env")

# Request configuration from environment
REQUEST_TIMEOUT = int(os.getenv("ODOO_REQUEST_TIMEOUT", "30"))
MAX_RETRIES = int(os.getenv("ODOO_MAX_RETRIES", "3"))


class OdooClient:
    """Client for interacting with Odoo JSON-2 API with retry logic and connection pooling"""

    def __init__(self, url: str, database: str, api_key: str):
        self.url = url.rstrip("/")
        self.database = database
        self.api_key = api_key

        # Create session with retry logic
        self.session = self._create_session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "X-Odoo-Database": database,
            "Content-Type": "application/json"
        })

        logger.info(f"Initialized OdooClient for {database} at {url}")

    def _create_session(self) -> requests.Session:
        """Create a requests session with retry logic and connection pooling"""
        session = requests.Session()

        # Configure retry strategy
        retry_strategy = Retry(
            total=MAX_RETRIES,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS", "TRACE"],
            backoff_factor=1  # Will retry after 1, 2, 4 seconds
        )

        # Mount adapter with retry strategy
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        return session

    def _make_request(self, model: str, method: str, payload: dict) -> Any:
        """Make a request to the Odoo API with timeout and error handling"""
        endpoint = f"{self.url}/json/2/{model}/{method}"

        start_time = time.time()
        logger.debug(f"Making request to {endpoint} with payload: {json.dumps(payload, default=str)[:200]}...")

        try:
            response = self.session.post(
                endpoint,
                json=payload,
                timeout=REQUEST_TIMEOUT
            )

            elapsed = time.time() - start_time
            logger.debug(f"Request completed in {elapsed:.2f}s with status {response.status_code}")

            response.raise_for_status()
            result = response.json()

            # Validate response structure
            if isinstance(result, dict) and 'error' in result:
                error_msg = result.get('error', {}).get('message', 'Unknown error')
                logger.error(f"Odoo API returned error: {error_msg}")
                raise ValueError(f"Odoo API error: {error_msg}")

            return result

        except requests.exceptions.Timeout:
            logger.error(f"Request timeout after {REQUEST_TIMEOUT}s for {endpoint}")
            raise TimeoutError(f"Request to Odoo API timed out after {REQUEST_TIMEOUT}s")

        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error to {endpoint}: {e}")
            raise ConnectionError(f"Failed to connect to Odoo API: {e}")

        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error {e.response.status_code} for {endpoint}: {e}")
            try:
                error_detail = e.response.json()
                raise ValueError(f"Odoo API HTTP {e.response.status_code}: {error_detail}")
            except json.JSONDecodeError:
                raise ValueError(f"Odoo API HTTP {e.response.status_code}: {e.response.text}")

        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed for {endpoint}: {e}")
            raise

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from {endpoint}: {e}")
            raise ValueError(f"Invalid JSON response from Odoo API: {e}")

        except Exception as e:
            logger.error(f"Unexpected error for {endpoint}: {e}")
            raise

    def search_read(
        self,
        model: str,
        domain: list,
        fields: Optional[list] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order: Optional[str] = None
    ) -> list:
        """Search and read records"""
        payload = {"domain": domain}
        if fields:
            payload["fields"] = fields
        if limit:
            payload["limit"] = limit
        if offset:
            payload["offset"] = offset
        if order:
            payload["order"] = order

        return self._make_request(model, "search_read", payload)

    def create(self, model: str, values: dict) -> int:
        """Create a new record"""
        payload = {"values": values}
        return self._make_request(model, "create", payload)

    def write(self, model: str, ids: list, values: dict) -> bool:
        """Update existing records"""
        payload = {"ids": ids, "values": values}
        return self._make_request(model, "write", payload)

    def unlink(self, model: str, ids: list) -> bool:
        """Delete records"""
        payload = {"ids": ids}
        return self._make_request(model, "unlink", payload)

    def search(
        self,
        model: str,
        domain: list,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order: Optional[str] = None
    ) -> list:
        """Search for record IDs"""
        payload = {"domain": domain}
        if limit:
            payload["limit"] = limit
        if offset:
            payload["offset"] = offset
        if order:
            payload["order"] = order

        return self._make_request(model, "search", payload)

    def read(self, model: str, ids: list, fields: Optional[list] = None) -> list:
        """Read specific records by ID"""
        payload = {"ids": ids}
        if fields:
            payload["fields"] = fields

        return self._make_request(model, "read", payload)

    def search_count(self, model: str, domain: list) -> int:
        """Count records matching domain"""
        payload = {"domain": domain}
        return self._make_request(model, "search_count", payload)


# Initialize the MCP server
app = Server("odoo-mcp-server")

# Store multiple company configurations (defaults from env)
company_configs: Dict[str, Dict[str, str]] = {}
odoo_clients: Dict[str, OdooClient] = {}

# Per-session user API key (set during SSE connection)
_session_api_key: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_session_api_key", default=None
)


def load_company_configs() -> Dict[str, Dict[str, str]]:
    """Load company configurations from INI file or environment variables"""
    global company_configs

    if company_configs:
        return company_configs

    # Try INI config file first
    if os.path.exists(CONFIG_FILE):
        config = ConfigParser()
        config.read(CONFIG_FILE)

        for section in config.sections():
            company_configs[section] = {
                'url': config.get(section, 'ODOO_URL'),
                'database': config.get(section, 'ODOO_DATABASE'),
                'api_key': config.get(section, 'ODOO_API_KEY'),
                'company_id': config.get(section, 'COMPANY_ID', fallback='1')
            }

    # Fallback to environment variables (for containerized deployments)
    if not company_configs:
        odoo_url = os.getenv("ODOO_URL")
        odoo_db = os.getenv("ODOO_DATABASE")
        # ODOO_API_KEY is optional — per-user keys are injected at runtime
        odoo_key = os.getenv("ODOO_API_KEY", "")

        if odoo_url and odoo_db:
            company_configs["mint"] = {
                'url': odoo_url,
                'database': odoo_db,
                'api_key': odoo_key,  # May be empty; per-user key overrides in get_odoo_client
                'company_id': os.getenv("COMPANY_ID", "1")
            }
            logger.info("Loaded company config from environment variables")
        else:
            raise ValueError(
                "No configuration found. Provide an INI config file or set "
                "ODOO_URL and ODOO_DATABASE environment variables."
            )

    logger.info(f"Loaded {len(company_configs)} company configurations: {list(company_configs.keys())}")
    return company_configs


def get_odoo_client(company: str) -> OdooClient:
    """Get or create Odoo client instance for specific company.

    If a per-session API key is set (via SSE auth), creates a user-scoped
    client using that key. This ensures each user only gets their Odoo
    permissions. Clients are cached by (company, api_key) to avoid
    re-creating sessions on every tool call.
    """
    global odoo_clients

    session_key = _session_api_key.get()
    cache_key = f"{company}:{session_key}" if session_key else company

    if cache_key not in odoo_clients:
        configs = load_company_configs()

        if company not in configs:
            available = ", ".join(configs.keys())
            raise ValueError(f"Company '{company}' not found. Available companies: {available}")

        config = configs[company]

        # Use the per-session API key if available, otherwise fall back to env config
        api_key = session_key or config['api_key']

        odoo_clients[cache_key] = OdooClient(
            config['url'],
            config['database'],
            api_key
        )
        logger.info(f"Created Odoo client for company={company} (per-user={bool(session_key)})")

    return odoo_clients[cache_key]


def list_available_companies() -> list[str]:
    """Get list of available company names"""
    configs = load_company_configs()
    return list(configs.keys())


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available Odoo tools"""
    return [
        Tool(
            name="odoo_list_companies",
            description="List all available company configurations",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="odoo_search_read",
            description="Search and read records from an Odoo model. Combines search and read operations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "The company configuration name to use (as defined in .env file sections)"
                    },
                    "model": {
                        "type": "string",
                        "description": "The Odoo model name (e.g., 'res.partner', 'account.move', 'product.product')"
                    },
                    "domain": {
                        "type": "array",
                        "description": "Search domain as a list of criteria (e.g., [['name', '=', 'John']]). Use [] for all records.",
                        "default": []
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of field names to retrieve. If not specified, returns all fields."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of records to return"
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of records to skip"
                    },
                    "order": {
                        "type": "string",
                        "description": "Sorting order (e.g., 'name asc', 'create_date desc')"
                    }
                },
                "required": ["company", "model"]
            }
        ),
        Tool(
            name="odoo_create",
            description="Create a new record in an Odoo model",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "The company configuration name to use"
                    },
                    "model": {
                        "type": "string",
                        "description": "The Odoo model name (e.g., 'res.partner', 'account.move')"
                    },
                    "values": {
                        "type": "object",
                        "description": "Dictionary of field values for the new record"
                    }
                },
                "required": ["company", "model", "values"]
            }
        ),
        Tool(
            name="odoo_write",
            description="Update existing records in an Odoo model",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "The company configuration name to use"
                    },
                    "model": {
                        "type": "string",
                        "description": "The Odoo model name"
                    },
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "List of record IDs to update"
                    },
                    "values": {
                        "type": "object",
                        "description": "Dictionary of field values to update"
                    }
                },
                "required": ["company", "model", "ids", "values"]
            }
        ),
        Tool(
            name="odoo_unlink",
            description="Delete records from an Odoo model",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "The company configuration name to use"
                    },
                    "model": {
                        "type": "string",
                        "description": "The Odoo model name"
                    },
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "List of record IDs to delete"
                    }
                },
                "required": ["company", "model", "ids"]
            }
        ),
        Tool(
            name="odoo_search",
            description="Search for record IDs matching criteria (without reading full records)",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "The company configuration name to use"
                    },
                    "model": {
                        "type": "string",
                        "description": "The Odoo model name"
                    },
                    "domain": {
                        "type": "array",
                        "description": "Search domain as a list of criteria",
                        "default": []
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of IDs to return"
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of records to skip"
                    },
                    "order": {
                        "type": "string",
                        "description": "Sorting order"
                    }
                },
                "required": ["company", "model"]
            }
        ),
        Tool(
            name="odoo_read",
            description="Read specific records by their IDs",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "The company configuration name to use"
                    },
                    "model": {
                        "type": "string",
                        "description": "The Odoo model name"
                    },
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "List of record IDs to read"
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of field names to retrieve"
                    }
                },
                "required": ["company", "model", "ids"]
            }
        ),
        Tool(
            name="odoo_search_count",
            description="Count the number of records matching search criteria",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "The company configuration name to use"
                    },
                    "model": {
                        "type": "string",
                        "description": "The Odoo model name"
                    },
                    "domain": {
                        "type": "array",
                        "description": "Search domain as a list of criteria",
                        "default": []
                    }
                },
                "required": ["company", "model"]
            }
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Handle tool calls"""
    try:
        if name == "odoo_list_companies":
            companies = list_available_companies()
            return [TextContent(
                type="text",
                text=f"Available companies: {', '.join(companies)}\n\nTotal: {len(companies)}"
            )]

        # All other tools require a company parameter
        company = arguments.get("company")
        if not company:
            raise ValueError("Company parameter is required")

        client = get_odoo_client(company)

        if name == "odoo_search_read":
            result = client.search_read(
                model=arguments["model"],
                domain=arguments.get("domain", []),
                fields=arguments.get("fields"),
                limit=arguments.get("limit"),
                offset=arguments.get("offset"),
                order=arguments.get("order")
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "odoo_create":
            result = client.create(
                model=arguments["model"],
                values=arguments["values"]
            )
            return [TextContent(type="text", text=f"Created record with ID: {result}")]

        elif name == "odoo_write":
            result = client.write(
                model=arguments["model"],
                ids=arguments["ids"],
                values=arguments["values"]
            )
            return [TextContent(type="text", text=f"Updated successfully: {result}")]

        elif name == "odoo_unlink":
            result = client.unlink(
                model=arguments["model"],
                ids=arguments["ids"]
            )
            return [TextContent(type="text", text=f"Deleted successfully: {result}")]

        elif name == "odoo_search":
            result = client.search(
                model=arguments["model"],
                domain=arguments.get("domain", []),
                limit=arguments.get("limit"),
                offset=arguments.get("offset"),
                order=arguments.get("order")
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "odoo_read":
            result = client.read(
                model=arguments["model"],
                ids=arguments["ids"],
                fields=arguments.get("fields")
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "odoo_search_count":
            result = client.search_count(
                model=arguments["model"],
                domain=arguments.get("domain", [])
            )
            return [TextContent(type="text", text=f"Count: {result}")]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        logger.error(f"Error executing tool {name}: {e}")
        return [TextContent(type="text", text=f"Error: {str(e)}")]


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validates Bearer token as an Odoo API key. Each user connects with their
    own key and gets only their Odoo permissions. Skips /health."""

    # Cache verified API keys -> Odoo uid to avoid re-authenticating every request
    _verified_keys: Dict[str, int] = {}

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"error": "Unauthorized"}, status_code=401
            )

        user_api_key = auth_header[7:]  # Strip "Bearer "
        if not user_api_key:
            return JSONResponse(
                {"error": "Unauthorized"}, status_code=401
            )

        # Verify the API key against Odoo (with caching)
        if user_api_key not in self._verified_keys:
            odoo_url = os.getenv("ODOO_URL", "").rstrip("/")
            odoo_db = os.getenv("ODOO_DATABASE", "odoo")

            if not odoo_url:
                logger.error("ODOO_URL not configured")
                return JSONResponse(
                    {"error": "Server misconfigured"}, status_code=500
                )

            try:
                # Verify the API key by making a simple read call to Odoo.
                # Odoo API keys work as Bearer tokens on the JSON-2 API.
                # A successful call = valid key; 401/403 = invalid key.
                resp = requests.post(
                    f"{odoo_url}/json/2/res.users/search_read",
                    json={"domain": [["id", "=", -1]], "fields": ["id"], "limit": 1},
                    headers={
                        "Authorization": f"Bearer {user_api_key}",
                        "X-Odoo-Database": odoo_db,
                        "Content-Type": "application/json",
                    },
                    timeout=10
                )

                if resp.status_code in (401, 403):
                    logger.warning("Odoo API key auth failed (invalid key)")
                    return JSONResponse(
                        {"error": "Unauthorized"}, status_code=401
                    )

                resp.raise_for_status()
                # Key is valid — store uid=0 as placeholder (actual perms enforced by Odoo)
                self._verified_keys[user_api_key] = 0
                logger.info("Verified Odoo API key successfully")

            except requests.exceptions.RequestException as e:
                logger.error(f"Odoo auth check failed: {e}")
                return JSONResponse(
                    {"error": "Auth verification failed"}, status_code=502
                )

        # Store the user's API key in request state so tools use it
        request.state.user_api_key = user_api_key
        request.state.user_uid = self._verified_keys[user_api_key]

        return await call_next(request)


async def health(request: Request) -> JSONResponse:
    """Health check endpoint"""
    try:
        companies = list_available_companies()
    except Exception:
        companies = []

    return JSONResponse({
        "status": "ok",
        "server": "odoo-mcp-server",
        "companies": companies,
    })


def create_starlette_app(mcp_server: Server) -> Starlette:
    """Create a Starlette app that serves the MCP server over SSE.

    Each SSE connection extracts the user's Odoo API key from the Bearer token
    and injects a per-user OdooClient into the global company_configs so that
    all tool calls within that session use the caller's permissions.
    """
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        # Extract the user's API key (already validated by BearerAuthMiddleware)
        user_api_key = getattr(request.state, "user_api_key", None)
        user_uid = getattr(request.state, "user_uid", None)

        if user_api_key:
            # Set per-session API key via contextvar — tool calls in this
            # session will create an OdooClient scoped to this user's key
            _session_api_key.set(user_api_key)
            logger.info(f"SSE session started for uid={user_uid} with per-user API key")

        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options()
            )

    starlette_app = Starlette(
        debug=False,
        routes=[
            Route("/health", health),
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
        middleware=[
            Middleware(BearerAuthMiddleware),
        ],
    )

    return starlette_app


async def main_stdio():
    """Run the MCP server over stdio transport"""
    logger.info("Starting Odoo MCP Server (stdio transport)")
    logger.info(f"Configuration file: {CONFIG_FILE}")

    try:
        companies = list_available_companies()
        logger.info(f"Loaded {len(companies)} companies: {', '.join(companies)}")
    except Exception as e:
        logger.warning(f"Could not load company configurations on startup: {e}")
        logger.warning("Configurations will be loaded on first tool call")

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")

    if transport == "sse":
        port = int(os.getenv("PORT", "8080"))
        logger.info(f"Starting Odoo MCP Server (SSE transport on port {port})")

        try:
            companies = list_available_companies()
            logger.info(f"Loaded {len(companies)} companies: {', '.join(companies)}")
        except Exception as e:
            logger.warning(f"Could not load company configurations on startup: {e}")

        starlette_app = create_starlette_app(app)
        uvicorn.run(starlette_app, host="0.0.0.0", port=port)
    else:
        import asyncio
        asyncio.run(main_stdio())
