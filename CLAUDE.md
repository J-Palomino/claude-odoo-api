# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an MCP (Model Context Protocol) server that provides tools to interact with Odoo 19's External JSON-2 API. The server supports multiple company/instance configurations and exposes 8 tools for CRUD operations on Odoo databases: list_companies, search_read, create, write, unlink, search, read, and search_count.

## Architecture

### Core Components

**OdooClient Class** (`src/odoo_mcp_server.py:27-100`)
- Manages HTTP session with persistent headers for authentication
- Encapsulates all Odoo API interactions via `/json/2/{model}/{method}` endpoint pattern
- All methods use POST requests with JSON payloads
- Authentication via Bearer token (`Authorization: Bearer {api_key}`)
- Database selection via custom header (`X-Odoo-Database: {database}`)

**MCP Server** (`src/odoo_mcp_server.py`)
- Built on `mcp.server.Server` using stdio transport
- Tools are defined in `list_tools()` with JSON Schema validation
- Tool execution handled in `call_tool()` dispatcher
- Supports multiple company configurations via `company_configs` dict
- `odoo_clients` dict stores one client instance per company (lazy-initialized)

### Odoo API Pattern

All Odoo API calls follow this structure:
```
POST {ODOO_URL}/json/2/{model_name}/{method_name}
Headers:
  - Authorization: Bearer {api_key}
  - X-Odoo-Database: {database}
  - Content-Type: application/json
Body: JSON payload specific to the method
```

See `create_odoo_invoices.py` for working examples of search_read and create operations.

### Configuration

The server uses a `.env` file with INI-style sections for multi-company support:

```ini
[company1]
ODOO_URL=http://localhost:8069
ODOO_DATABASE=database_name
ODOO_API_KEY=api_key_here
COMPANY_ID=1  # Optional, defaults to 1

[company2]
ODOO_URL=http://localhost:8069
ODOO_DATABASE=another_db
ODOO_API_KEY=another_key
COMPANY_ID=2
```

Configuration loading:
- `load_company_configs()` reads all sections from `.env` using `configparser`
- `get_odoo_client(company)` creates/returns cached client for specific company
- `list_available_companies()` returns list of configured company names
- Environment variable `ODOO_CONFIG_FILE` can override default `.env` path

## Development Commands

### Local Development

```bash
# Setup
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Run the MCP server
python src/odoo_mcp_server.py

# Set environment variables
export ODOO_URL=http://localhost:8069
export ODOO_DATABASE=your_database
export ODOO_API_KEY=your_api_key
```

### Docker

```bash
# Build and run
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down

# Build image only
docker build -t odoo-mcp-server .

# Run with custom env vars
docker run -i --rm \
  -e ODOO_URL=http://localhost:8069 \
  -e ODOO_DATABASE=your_db \
  -e ODOO_API_KEY=your_key \
  odoo-mcp-server
```

### Testing API Connection

```bash
# Test Odoo accessibility
curl http://localhost:8069/web/database/selector

# Test API authentication
curl -X POST http://localhost:8069/json/2/res.partner/search_count \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "X-Odoo-Database: YOUR_DATABASE" \
  -H "Content-Type: application/json" \
  -d '{"domain": []}'
```

## Adding New MCP Tools

To add a new Odoo operation:

1. **Add method to `OdooClient` class** (if needed)
   - Follow pattern: build payload dict, call `_make_request(model, method, payload)`
   - Return the JSON response directly

2. **Add tool definition in `list_tools()`**
   - Define tool name (prefix with `odoo_`)
   - Provide clear description
   - Define inputSchema with JSON Schema format
   - **IMPORTANT:** Add `company` parameter as required for all Odoo operations
   - Mark other required parameters

3. **Add handler in `call_tool()`**
   - Extract `company` argument and get client: `client = get_odoo_client(company)`
   - Extract other arguments
   - Call corresponding `OdooClient` method
   - Return `TextContent` with JSON result or success message
   - Handle errors with descriptive messages

Example tool definition:
```python
Tool(
    name="odoo_my_operation",
    description="Description of operation",
    inputSchema={
        "type": "object",
        "properties": {
            "company": {
                "type": "string",
                "description": "The company configuration name"
            },
            # ... other parameters
        },
        "required": ["company", ...]
    }
)
```

## MCP Catalog Generation

To distribute the server via MCP catalogs (e.g., for the Docker Desktop MCP Toolkit), the `bmya-mcp-catalog.yaml` source file must be converted to `catalog.json`.

This process requires the `PyYAML` dependency, which has been added to `requirements.txt`.

The command to perform the conversion is:
```bash
python -c "import sys, yaml, json; json.dump(yaml.safe_load(open('bmya-mcp-catalog.yaml')), sys.stdout, indent=2)" > catalog.json
```

## Odoo Domain Filter Syntax

Domains are search criteria expressed as lists:
- Simple condition: `[["field", "operator", value]]`
- AND (implicit): `[["field1", "=", "A"], ["field2", "=", "B"]]`
- OR: `["|", ["field1", "=", "A"], ["field2", "=", "B"]]`
- NOT: `["!", ["field", "=", value]]`
- Nested: `["|", ["state", "=", "draft"], "&", ["amount", ">", 1000], ["partner_id", "!=", False]]`

Common operators: `=`, `!=`, `>`, `<`, `>=`, `<=`, `in`, `not in`, `ilike` (case-insensitive), `like`, `=ilike`, `=like`

## Key Odoo Models

Reference these common models when building queries:
- `res.partner` - Contacts/companies
- `account.move` - Invoices/bills
- `sale.order` - Sales orders
- `product.product` - Products
- `stock.picking` - Inventory transfers
- `project.task` - Tasks

## Important Notes

- External API requires Odoo Custom pricing plan (not available on Free/Standard)
- All API calls run in their own SQL transaction (auto-commit on success, rollback on error)
- When running in Docker with local Odoo, use `host.docker.internal` instead of `localhost` or `--network host`
- The MCP server runs in stdio mode - it reads from stdin and writes to stdout following MCP protocol

## Docker MCP Registry Submission

### Status
**PR #814 submitted to docker/mcp-registry on 2025-12-02**
- PR URL: https://github.com/docker/mcp-registry/pull/814
- Status: Pending review by Docker team

### Submission Process Completed

1. **Verified Dockerfile**: Confirmed existence of Dockerfile in repository (bmya/claude-odoo-api)

2. **Forked docker/mcp-registry**: Created fork at Danisan/mcp-registry

3. **Created Server Entry**: Added `servers/odoo-api/server.yaml` with:
   - Server name: `odoo-api`
   - Docker image: `bmya/odoo-mcp-server`
   - Category: business
   - Tags: odoo, erp, business, crm, secrets
   - Icon: https://www.google.com/s2/favicons?domain=odoo.com&sz=64
   - Source project: https://github.com/bmya/claude-odoo-api
   - Source commit: 5f93afd973bcda0386140c465e5fac8728f156b6

4. **Created tools.json**: Added comprehensive tool definitions to avoid build failures:
   - 8 tools documented: list_companies, search_read, create, write, unlink, search, read, search_count
   - Full argument specifications for each tool
   - Required because server needs configuration before it can list tools

5. **Submitted Pull Request**: PR #814 to docker/mcp-registry
   - Includes full feature description
   - Documents multi-company support
   - Notes MIT license compliance
   - Provides comprehensive tool list

### Configuration in Registry

The server entry requires three secrets for configuration:
```yaml
config:
  secrets:
    - name: odoo.url
      env: ODOO_URL
      example: http://localhost:8069
    - name: odoo.database
      env: ODOO_DATABASE
      example: your_database
    - name: odoo.api_key
      env: ODOO_API_KEY
      example: your_api_key_here
```

### Next Steps
1. Monitor PR #814 for CI validation results
2. Respond to any feedback from Docker team review
3. Provide test credentials if requested via https://forms.gle/6Lw3nsvu2d6nFg8e6
4. Once merged, server will be available in official Docker MCP Registry

### Files in Registry Submission
- `servers/odoo-api/server.yaml` - Server configuration and metadata
- `servers/odoo-api/tools.json` - Tool definitions for build process
