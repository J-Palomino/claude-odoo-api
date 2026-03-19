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


def _build_menu_tree(menus: list) -> list:
    """Build hierarchical menu tree from flat list of menu records."""
    by_id = {m["id"]: {**m, "children": []} for m in menus}
    roots = []
    for m in by_id.values():
        pid = m.get("parent_id")
        parent_key = pid[0] if isinstance(pid, (list, tuple)) else pid
        if parent_key and parent_key in by_id:
            by_id[parent_key]["children"].append(m)
        else:
            roots.append(m)
    return roots


def _resolve_view_id(val) -> Optional[int]:
    """Extract integer view ID from Odoo many2one field (may be [id, name] or int)."""
    if isinstance(val, (list, tuple)):
        return val[0] if val else None
    if isinstance(val, int) and val:
        return val
    return None


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
        ),

        # ── Website Tools ────────────────────────────────────────────

        Tool(
            name="website_list_pages",
            description="List website pages with URL, title, and published status. Supports filtering by URL pattern, name, or published state.",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "Company configuration name"},
                    "website_id": {"type": "integer", "description": "Filter by website ID (default: all)"},
                    "published_only": {"type": "boolean", "description": "Only return published pages", "default": False},
                    "url_pattern": {"type": "string", "description": "Filter pages whose URL contains this string (case-insensitive)"},
                    "name_pattern": {"type": "string", "description": "Filter pages whose name contains this string (case-insensitive)"},
                    "limit": {"type": "integer", "description": "Max records to return", "default": 50},
                    "offset": {"type": "integer", "description": "Number of records to skip"}
                },
                "required": ["company"]
            }
        ),
        Tool(
            name="website_get_page",
            description="Get a website page's full details including HTML content from its linked ir.ui.view. Lookup by page ID or exact URL.",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "Company configuration name"},
                    "page_id": {"type": "integer", "description": "Page record ID"},
                    "url": {"type": "string", "description": "Exact page URL (e.g. '/about-us')"}
                },
                "required": ["company"]
            }
        ),
        Tool(
            name="website_create_page",
            description="Create a new website page with optional HTML content. Automatically creates the backing ir.ui.view and wraps content in the website layout template.",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "Company configuration name"},
                    "name": {"type": "string", "description": "Page title"},
                    "url": {"type": "string", "description": "Page URL path (e.g. '/my-page')"},
                    "content": {"type": "string", "description": "HTML body content (auto-wrapped in QWeb layout)"},
                    "is_published": {"type": "boolean", "description": "Publish immediately", "default": True},
                    "website_id": {"type": "integer", "description": "Website ID", "default": 1},
                    "meta_title": {"type": "string", "description": "SEO meta title"},
                    "meta_description": {"type": "string", "description": "SEO meta description"}
                },
                "required": ["company", "name", "url"]
            }
        ),
        Tool(
            name="website_update_page",
            description="Update a website page's metadata (name, URL, SEO fields, published state) and/or its HTML content. Pass only the fields you want to change.",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "Company configuration name"},
                    "page_id": {"type": "integer", "description": "Page record ID to update"},
                    "name": {"type": "string", "description": "New page title"},
                    "url": {"type": "string", "description": "New URL path"},
                    "is_published": {"type": "boolean", "description": "Set published state"},
                    "content": {"type": "string", "description": "New HTML content (replaces entire arch_db on the linked view)"},
                    "meta_title": {"type": "string", "description": "SEO meta title"},
                    "meta_description": {"type": "string", "description": "SEO meta description"},
                    "meta_keywords": {"type": "string", "description": "SEO meta keywords"},
                    "meta_og_img": {"type": "string", "description": "Open Graph image URL"}
                },
                "required": ["company", "page_id"]
            }
        ),
        Tool(
            name="website_toggle_published",
            description="Publish or unpublish a website page.",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "Company configuration name"},
                    "page_id": {"type": "integer", "description": "Page record ID"},
                    "published": {"type": "boolean", "description": "True to publish, False to unpublish"}
                },
                "required": ["company", "page_id", "published"]
            }
        ),
        Tool(
            name="website_list_menus",
            description="List website menu items. Returns a hierarchical tree by default, or a flat list.",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "Company configuration name"},
                    "website_id": {"type": "integer", "description": "Website ID", "default": 1},
                    "flat": {"type": "boolean", "description": "Return flat list instead of tree", "default": False}
                },
                "required": ["company"]
            }
        ),
        Tool(
            name="website_create_menu",
            description="Create a new menu item. Can specify parent by ID or by name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "Company configuration name"},
                    "name": {"type": "string", "description": "Menu item label"},
                    "url": {"type": "string", "description": "Menu item URL or path"},
                    "website_id": {"type": "integer", "description": "Website ID", "default": 1},
                    "parent_id": {"type": "integer", "description": "Parent menu ID"},
                    "parent_name": {"type": "string", "description": "Find parent menu by name (alternative to parent_id)"},
                    "sequence": {"type": "integer", "description": "Sort order (lower = first)", "default": 50},
                    "new_window": {"type": "boolean", "description": "Open link in new window", "default": False}
                },
                "required": ["company", "name", "url"]
            }
        ),
        Tool(
            name="website_update_menu",
            description="Update an existing menu item (rename, move, reorder, change URL, toggle visibility).",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "Company configuration name"},
                    "menu_id": {"type": "integer", "description": "Menu item ID to update"},
                    "name": {"type": "string", "description": "New label"},
                    "url": {"type": "string", "description": "New URL"},
                    "parent_id": {"type": "integer", "description": "Move under a different parent menu"},
                    "sequence": {"type": "integer", "description": "New sort order"},
                    "new_window": {"type": "boolean", "description": "Open in new window"},
                    "is_visible": {"type": "boolean", "description": "Toggle visibility"}
                },
                "required": ["company", "menu_id"]
            }
        ),
        Tool(
            name="website_delete_menu",
            description="Delete a menu item by ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "Company configuration name"},
                    "menu_id": {"type": "integer", "description": "Menu item ID to delete"}
                },
                "required": ["company", "menu_id"]
            }
        ),
        Tool(
            name="website_manage_redirect",
            description="Manage URL redirects (301/302). Actions: 'list' to view all, 'create' to add, 'delete' to remove.",
            inputSchema={
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "Company configuration name"},
                    "action": {"type": "string", "enum": ["list", "create", "delete"], "description": "Operation to perform"},
                    "website_id": {"type": "integer", "description": "Website ID", "default": 1},
                    "limit": {"type": "integer", "description": "Max records for 'list'", "default": 50},
                    "url_from": {"type": "string", "description": "Source URL (for 'create')"},
                    "url_to": {"type": "string", "description": "Target URL (for 'create')"},
                    "redirect_type": {"type": "string", "enum": ["301", "302"], "description": "Redirect type (for 'create')", "default": "301"},
                    "redirect_id": {"type": "integer", "description": "Redirect ID (for 'delete')"}
                },
                "required": ["company", "action"]
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

        # ── Website Tool Handlers ────────────────────────────────────

        elif name == "website_list_pages":
            domain = []
            if arguments.get("website_id"):
                domain.append(["website_id", "=", arguments["website_id"]])
            if arguments.get("published_only"):
                domain.append(["is_published", "=", True])
            if arguments.get("url_pattern"):
                domain.append(["url", "ilike", arguments["url_pattern"]])
            if arguments.get("name_pattern"):
                domain.append(["name", "ilike", arguments["name_pattern"]])

            result = client.search_read(
                model="website.page",
                domain=domain,
                fields=["id", "name", "url", "is_published", "website_id",
                        "website_meta_title", "website_meta_description", "date_publish"],
                limit=arguments.get("limit", 50),
                offset=arguments.get("offset"),
                order="url asc"
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "website_get_page":
            page_id = arguments.get("page_id")
            url = arguments.get("url")

            if not page_id and not url:
                raise ValueError("Provide either page_id or url")

            page_fields = ["id", "name", "url", "is_published", "view_id",
                           "website_id", "website_meta_title", "website_meta_description",
                           "website_meta_keywords", "website_meta_og_img", "date_publish"]

            if url:
                pages = client.search_read("website.page", [["url", "=", url]],
                                           fields=page_fields, limit=1)
                if not pages:
                    raise ValueError(f"No page found with URL: {url}")
                page = pages[0]
            else:
                pages = client.read("website.page", [page_id], fields=page_fields)
                if not pages:
                    raise ValueError(f"No page found with ID: {page_id}")
                page = pages[0]

            # Fetch linked view content
            vid = _resolve_view_id(page.get("view_id"))
            if vid:
                views = client.read("ir.ui.view", [vid], fields=["id", "name", "arch_db", "key"])
                if views:
                    page["view_content"] = views[0].get("arch_db", "")
                    page["view_key"] = views[0].get("key", "")

            return [TextContent(type="text", text=json.dumps(page, indent=2, default=str))]

        elif name == "website_create_page":
            page_name = arguments["name"]
            page_url = arguments["url"]
            if not page_url.startswith("/"):
                page_url = "/" + page_url

            content = arguments.get("content", "")
            website_id = arguments.get("website_id", 1)
            is_published = arguments.get("is_published", True)

            # Generate view key from URL
            url_slug = page_url.strip("/").replace("/", "_").replace("-", "_")
            view_key = f"website.page_{url_slug}"

            # Wrap content in QWeb layout template
            arch = (
                f'<t t-name="{view_key}">'
                f'<t t-call="website.layout">'
                f'<div id="wrap" class="oe_structure oe_empty">{content}</div>'
                f'</t></t>'
            )

            # Create the backing view first
            view_id = client.create("ir.ui.view", {
                "name": page_name,
                "type": "qweb",
                "arch_db": arch,
                "key": view_key
            })

            # Create the page record
            page_values = {
                "name": page_name,
                "url": page_url,
                "is_published": is_published,
                "website_id": website_id,
                "view_id": view_id
            }
            if arguments.get("meta_title"):
                page_values["website_meta_title"] = arguments["meta_title"]
            if arguments.get("meta_description"):
                page_values["website_meta_description"] = arguments["meta_description"]

            new_page_id = client.create("website.page", page_values)

            return [TextContent(type="text", text=json.dumps({
                "page_id": new_page_id,
                "view_id": view_id,
                "url": page_url,
                "view_key": view_key,
                "message": f"Created page '{page_name}' at {page_url}"
            }, indent=2))]

        elif name == "website_update_page":
            pid = arguments["page_id"]

            # Build page field updates
            page_values = {}
            field_map = {
                "name": "name", "url": "url", "is_published": "is_published",
                "meta_title": "website_meta_title", "meta_description": "website_meta_description",
                "meta_keywords": "website_meta_keywords", "meta_og_img": "website_meta_og_img"
            }
            for arg_key, odoo_field in field_map.items():
                if arg_key in arguments and arguments[arg_key] is not None:
                    page_values[odoo_field] = arguments[arg_key]

            if page_values:
                client.write("website.page", [pid], page_values)

            # Update view content if provided
            if "content" in arguments and arguments["content"] is not None:
                pages = client.read("website.page", [pid], fields=["view_id"])
                if not pages:
                    raise ValueError(f"Page {pid} not found")
                vid = _resolve_view_id(pages[0].get("view_id"))
                if not vid:
                    raise ValueError(f"Page {pid} has no linked view")
                client.write("ir.ui.view", [vid], {"arch_db": arguments["content"]})

            updated = list(page_values.keys())
            if "content" in arguments and arguments["content"] is not None:
                updated.append("arch_db (view content)")
            return [TextContent(type="text", text=f"Updated page {pid}: {', '.join(updated) if updated else 'no changes'}")]

        elif name == "website_toggle_published":
            pid = arguments["page_id"]
            published = arguments["published"]
            client.write("website.page", [pid], {"is_published": published})
            state = "published" if published else "unpublished"
            return [TextContent(type="text", text=f"Page {pid} is now {state}")]

        elif name == "website_list_menus":
            website_id = arguments.get("website_id", 1)
            flat = arguments.get("flat", False)

            menus = client.search_read(
                model="website.menu",
                domain=[["website_id", "=", website_id]],
                fields=["id", "name", "url", "sequence", "parent_id",
                        "child_id", "new_window", "is_visible", "page_id"],
                order="sequence asc"
            )

            if flat:
                return [TextContent(type="text", text=json.dumps(menus, indent=2, default=str))]

            tree = _build_menu_tree(menus)
            return [TextContent(type="text", text=json.dumps(tree, indent=2, default=str))]

        elif name == "website_create_menu":
            website_id = arguments.get("website_id", 1)
            parent_id = arguments.get("parent_id")

            # Resolve parent by name if needed
            if not parent_id and arguments.get("parent_name"):
                parents = client.search("website.menu", [
                    ["website_id", "=", website_id],
                    ["name", "ilike", arguments["parent_name"]]
                ], limit=1)
                if parents:
                    parent_id = parents[0]

            # Fall back to root menu
            if not parent_id:
                roots = client.search("website.menu", [
                    ["website_id", "=", website_id],
                    ["parent_id", "=", False]
                ], limit=1)
                if roots:
                    parent_id = roots[0]

            values = {
                "name": arguments["name"],
                "url": arguments["url"],
                "website_id": website_id,
                "sequence": arguments.get("sequence", 50),
                "new_window": arguments.get("new_window", False)
            }
            if parent_id:
                values["parent_id"] = parent_id

            menu_id = client.create("website.menu", values)
            return [TextContent(type="text", text=json.dumps({
                "menu_id": menu_id,
                "parent_id": parent_id,
                "message": f"Created menu '{arguments['name']}' → {arguments['url']}"
            }, indent=2))]

        elif name == "website_update_menu":
            mid = arguments["menu_id"]
            values = {}
            for key in ("name", "url", "parent_id", "sequence", "new_window", "is_visible"):
                if key in arguments and arguments[key] is not None:
                    values[key] = arguments[key]

            if not values:
                return [TextContent(type="text", text=f"No changes specified for menu {mid}")]

            client.write("website.menu", [mid], values)
            return [TextContent(type="text", text=f"Updated menu {mid}: {', '.join(values.keys())}")]

        elif name == "website_delete_menu":
            mid = arguments["menu_id"]
            client.unlink("website.menu", [mid])
            return [TextContent(type="text", text=f"Deleted menu {mid}")]

        elif name == "website_manage_redirect":
            action = arguments["action"]
            website_id = arguments.get("website_id", 1)
            redirect_model = "website.redirect"

            try:
                if action == "list":
                    result = client.search_read(
                        model=redirect_model,
                        domain=[["website_id", "=", website_id]],
                        fields=["id", "name", "url_from", "url_to", "redirect_type", "active"],
                        limit=arguments.get("limit", 50),
                        order="id desc"
                    )
                    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

                elif action == "create":
                    url_from = arguments.get("url_from")
                    url_to = arguments.get("url_to")
                    if not url_from or not url_to:
                        raise ValueError("url_from and url_to are required for 'create'")
                    rid = client.create(redirect_model, {
                        "url_from": url_from,
                        "url_to": url_to,
                        "redirect_type": arguments.get("redirect_type", "301"),
                        "website_id": website_id
                    })
                    return [TextContent(type="text", text=f"Created redirect {rid}: {url_from} → {url_to}")]

                elif action == "delete":
                    rid = arguments.get("redirect_id")
                    if not rid:
                        raise ValueError("redirect_id is required for 'delete'")
                    client.unlink(redirect_model, [rid])
                    return [TextContent(type="text", text=f"Deleted redirect {rid}")]

            except ValueError as e:
                # Model might be named differently in this Odoo version
                if "not found" in str(e).lower() or "does not exist" in str(e).lower():
                    return [TextContent(type="text",
                        text=f"Model '{redirect_model}' not found. Try using the generic "
                             f"odoo_search_read tool with model 'website.rewrite' instead.\n"
                             f"Original error: {e}")]
                raise

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
