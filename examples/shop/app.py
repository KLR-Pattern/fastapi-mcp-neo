"""The fastapi-mcp-neo demo app: a small shop with users, catalog, orders and stats.

Feature coverage on purpose:

- tag domains: ``shop:catalog`` / ``shop:orders`` / ``iam:users`` / ``analytics``
- **recursive model → true depth** (``/categories``): selecting ``children``
  once returns the whole subtree — the selection you write repeats per
  level, the tree comes back at the depth the route computed (no invisible
  truncation). The type's schema description states the contract.
- Query Parameter Model (``Annotated[ProductFilter, Query()]``)
- auth via ``x-token`` header (orders + writes need ``demo-secret``)
- collection/item route pairs with distinct endpoint function names
- **descriptions everywhere** — model docstrings, ``Field(description=...)``,
  endpoint docstrings, ``Query()/Path()/Body(description=...)`` all flow into
  the GraphQL schema (hover in GraphiQL to see them)
- response filtering → **JSON scalar passthrough** (``/products/{id}/tag`` uses
  ``response_model_exclude_unset``: the JSON may lack keys the model declares,
  so the bridge exposes it as a raw JSON blob with an explanatory note instead
  of promising fields that filtering can remove)
- ``deprecated=True`` → **GraphQL-native ``@deprecated``** (``/v1/orders``:
  struck through in GraphiQL, hidden from default introspection, executable)
- Form/File route → **explicit skip** (``/products/{id}/image``: multipart has
  no MCP input channel, so the scanner drops it with a logged reason — the
  route itself keeps working over plain HTTP)
- one untyped route (``/now``) to demonstrate the degraded-JSON bridge:
  it stays callable as a raw JSON field, and the startup notice names it
  (add a return annotation to regain a structured type)
- **startup-notice showcase**: one route per remaining notice reason —
  union response (``/lookup/{ref}``) and union model field (``/search/{term}``)
  bridge as raw JSON; raw ``Response`` return (``/banner.txt``), required
  header param (``/telemetry``), hidden route (``/internal/metrics``),
  query model mixed with plain params (``/price-watch``) and an unsupported
  input type (``/alerts``, ``set[int]``) skip. Boot the server and read
  the log lines.
- lifespan startup log (proves lifespan wiring)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Path,
    Query,
)
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

DEMO_TOKEN = "demo-secret"


# --------------------------------------------------------------------- schemas


class UserOut(BaseModel):
    """An account holder of the shop."""

    id: int = Field(description="stable user identifier")
    name: str = Field(description="display name")
    email: str = Field(description="contact address")
    role: Literal["admin", "member"] = Field(
        default="member", description="permission group"
    )


class UserCreate(BaseModel):
    """Payload for registering a user."""

    name: str = Field(description="display name")
    email: str = Field(description="contact address")
    role: Literal["admin", "member"] = Field(
        default="member", description="permission group"
    )


class UserFilter(BaseModel):
    """Filters for browsing users (Query Parameter Model)."""

    role: Literal["admin", "member"] | None = Field(
        default=None, description="only users with this role"
    )
    limit: int = Field(default=10, ge=1, le=100, description="max users returned")


class ProductOut(BaseModel):
    """A catalog product as exposed to customers."""

    id: int = Field(description="stable product identifier")
    name: str = Field(description="human-readable product name")
    category: str = Field(description="merchandising category, e.g. coffee")
    price_cents: int = Field(description="unit price in minor currency units")
    in_stock: bool = Field(description="available for purchase right now")


class ProductCreate(BaseModel):
    """Payload for adding a product to the catalog."""

    name: str = Field(description="human-readable product name")
    category: str = Field(description="merchandising category, e.g. coffee")
    price_cents: int = Field(description="unit price in minor currency units")


class ProductFilter(BaseModel):
    """Filters for browsing the catalog (Query Parameter Model)."""

    category: str | None = Field(default=None, description="exact category match")
    in_stock: bool | None = Field(default=None, description="stock availability")
    limit: int = Field(default=10, ge=1, le=100, description="max products returned")


class OrderOut(BaseModel):
    """A purchase order connecting a user to a product."""

    id: int = Field(description="stable order identifier")
    user_id: int = Field(description="the buying user")
    product_id: int = Field(description="the purchased product")
    quantity: int = Field(description="units purchased")
    status: str = Field(description="lifecycle state: created / paid / shipped")


class OrderCreate(BaseModel):
    """Payload for placing an order."""

    product_id: int = Field(description="product to purchase")
    quantity: int = Field(default=1, ge=1, description="units to purchase")


class ShopStats(BaseModel):
    """Aggregated shop health metrics."""

    total_orders: int = Field(description="orders placed, all statuses")
    revenue_cents: int = Field(description="sum of quantity * unit price")
    products_in_stock: int = Field(description="products currently purchasable")


class ProductTag(BaseModel):
    """Shelf price tag for a product — deliberately sparse on purpose."""

    id: int = Field(description="stable product identifier")
    name: str = Field(description="human-readable name")
    price_cents: int = Field(description="unit price in minor currency units")
    promo_code: str | None = Field(
        default=None, description="active discount code, when on sale"
    )
    featured: bool = Field(default=False, description="highlighted on the front page")


class CategoryOut(BaseModel):
    """A catalog category node — categories nest, so the model is recursive."""

    name: str = Field(description="category name, unique within its parent")
    children: list[CategoryOut] = Field(
        default_factory=list, description="nested subcategories"
    )


CategoryOut.model_rebuild()


class SearchHit(BaseModel):
    """One search result — a user or a product, whichever matched first."""

    hit: UserOut | ProductOut = Field(
        description="the matched record; union field, exposed as raw JSON"
    )


class AlertCreate(BaseModel):
    """Payload for registering price alerts."""

    watched: set[int] = Field(description="product ids to watch (JSON array)")


class PriceFilter(BaseModel):
    """Price-side filters (Query Parameter Model — mixed showcase)."""

    currency: str | None = Field(default=None, description="iso currency code")


# ------------------------------------------------------------------------ auth


def verify_token(x_token: Annotated[str | None, Header()] = None) -> str:
    if x_token != DEMO_TOKEN:
        raise HTTPException(
            status_code=401, detail=f"invalid or missing x-token (use {DEMO_TOKEN=!r})"
        )
    return x_token


# ------------------------------------------------------------------------ app


def create_app() -> FastAPI:
    """Build the demo app (fresh per call — tests rely on this)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.started_at = datetime.now(timezone.utc)
        print("[demo] startup — data is in-memory, mutations live until restart")
        yield
        print("[demo] shutdown")

    app = FastAPI(title="fastapi-mcp-neo demo", version="0.3.0", lifespan=lifespan)
    app.state.users = {
        1: {"id": 1, "name": "alice", "email": "alice@shop.io", "role": "admin"},
        2: {"id": 2, "name": "bob", "email": "bob@shop.io", "role": "member"},
        3: {"id": 3, "name": "carol", "email": "carol@shop.io", "role": "member"},
    }
    app.state.products = {
        1: {"id": 1, "name": "espresso machine", "category": "coffee",
            "price_cents": 12900, "in_stock": True},
        2: {"id": 2, "name": "bur grinder", "category": "coffee",
            "price_cents": 8900, "in_stock": False},
        3: {"id": 3, "name": "milk frother", "category": "coffee",
            "price_cents": 2400, "in_stock": True},
        4: {"id": 4, "name": "brew scale", "category": "gear",
            "price_cents": 3100, "in_stock": True},
    }
    app.state.categories = [
        CategoryOut(
            name="coffee",
            children=[
                CategoryOut(
                    name="filter", children=[CategoryOut(name="pour-over")]
                ),
                CategoryOut(name="espresso"),
            ],
        ),
        CategoryOut(name="tea", children=[CategoryOut(name="green")]),
    ]
    app.state.orders = {
        1: {"id": 1, "user_id": 1, "product_id": 1, "quantity": 1, "status": "paid"},
        2: {"id": 2, "user_id": 2, "product_id": 3, "quantity": 2, "status": "shipped"},
    }
    app.state.next_id = 100

    def _next(app: FastAPI) -> int:
        app.state.next_id += 1
        return app.state.next_id

    # ------------------------------------------------------------------ users

    @app.get("/users", response_model=list[UserOut], tags=["iam:users"])
    async def list_users(
        filters: Annotated[UserFilter, Query(description="how to narrow the user list")],
    ) -> list[UserOut]:
        """Browse shop users.

        Newest users first is NOT guaranteed; use ``role`` + ``limit`` to narrow.
        """
        users = app.state.users.values()
        if filters.role is not None:
            users = (u for u in users if u["role"] == filters.role)
        return [UserOut.model_validate(u) for u in list(users)[: filters.limit]]

    @app.get("/users/{user_id}", response_model=UserOut, tags=["iam:users"])
    async def get_user(
        user_id: Annotated[int, Path(description="the user to fetch")],
    ) -> UserOut:
        """Fetch a single user by id.

        Returns 404-shaped field errors for unknown ids.
        """
        user = app.state.users.get(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        return UserOut.model_validate(user)

    @app.post("/users", response_model=UserOut, tags=["iam:users"])
    async def create_user(
        payload: Annotated[UserCreate, Body(description="the account to register")],
        _token: str = Depends(verify_token),
    ) -> UserOut:
        """Register a user (requires x-token)."""
        user = {"id": _next(app), **payload.model_dump()}
        app.state.users[user["id"]] = user
        return UserOut.model_validate(user)

    # --------------------------------------------------------------- products

    @app.get("/products", response_model=list[ProductOut], tags=["shop:catalog"])
    async def list_products(
        filters: Annotated[ProductFilter, Query(description="catalog filters")],
    ) -> list[ProductOut]:
        """Browse the product catalog.

        Filter by category and stock; results are capped by ``limit``.
        """
        products = app.state.products.values()
        if filters.category is not None:
            products = (p for p in products if p["category"] == filters.category)
        if filters.in_stock is not None:
            products = (p for p in products if p["in_stock"] == filters.in_stock)
        return [ProductOut.model_validate(p) for p in list(products)[: filters.limit]]

    @app.get("/products/{product_id}", response_model=ProductOut, tags=["shop:catalog"])
    async def get_product(
        product_id: Annotated[int, Path(description="the product to fetch")],
    ) -> ProductOut:
        """Fetch a single product by id."""
        product = app.state.products.get(product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="product not found")
        return ProductOut.model_validate(product)

    @app.get("/categories", response_model=list[CategoryOut], tags=["shop:catalog"])
    async def list_categories() -> list[CategoryOut]:
        """Browse the category tree.

        Recursion showcase: over MCP, one level of ``children`` selection
        returns the complete subtree at its true depth.
        """
        return app.state.categories

    @app.post("/products", response_model=ProductOut, tags=["shop:catalog"])
    async def create_product(
        payload: Annotated[ProductCreate, Body(description="the product to add")],
        _token: str = Depends(verify_token),
    ) -> ProductOut:
        """Add a product to the catalog (requires x-token).

        New products start in stock.
        """
        product = {"id": _next(app), "in_stock": True, **payload.model_dump()}
        app.state.products[product["id"]] = product
        return ProductOut.model_validate(product)

    # ----------------------------------------------------------------- orders

    @app.get("/orders", response_model=list[OrderOut], tags=["shop:orders"])
    async def list_orders(
        status: Annotated[
            str | None, Query(description="filter by order status, e.g. paid")
        ] = None,
        _token: str = Depends(verify_token),
    ) -> list[OrderOut]:
        """List orders (requires x-token)."""
        orders = app.state.orders.values()
        if status is not None:
            orders = (o for o in orders if o["status"] == status)
        return [OrderOut.model_validate(o) for o in orders]

    @app.get("/orders/{order_id}", response_model=OrderOut, tags=["shop:orders"])
    async def get_order(
        order_id: Annotated[int, Path(description="the order to fetch")],
        _token: str = Depends(verify_token),
    ) -> OrderOut:
        """Fetch a single order by id (requires x-token)."""
        order = app.state.orders.get(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return OrderOut.model_validate(order)

    @app.post("/orders", response_model=OrderOut, tags=["shop:orders"])
    async def create_order(
        payload: Annotated[OrderCreate, Body(description="the order to place")],
        _token: str = Depends(verify_token),
    ) -> OrderOut:
        """Place an order (requires x-token).

        Rejects unknown products (400) and out-of-stock ones (409).
        """
        product = app.state.products.get(payload.product_id)
        if product is None:
            raise HTTPException(status_code=400, detail="unknown product")
        if not product["in_stock"]:
            raise HTTPException(status_code=409, detail="product out of stock")
        order = {"id": _next(app), "user_id": 1, "status": "created", **payload.model_dump()}
        app.state.orders[order["id"]] = order
        return OrderOut.model_validate(order)

    # ------------------------------------------------------------------ stats

    @app.get("/stats", response_model=ShopStats, tags=["analytics"])
    async def shop_stats() -> ShopStats:
        """Shop health metrics, computed live over orders and stock."""
        orders = list(app.state.orders.values())
        prices = app.state.products
        return ShopStats(
            total_orders=len(orders),
            revenue_cents=sum(
                o["quantity"] * prices[o["product_id"]]["price_cents"] for o in orders
            ),
            products_in_stock=sum(1 for p in app.state.products.values() if p["in_stock"]),
        )

    # ------------------------------------------------------------ misc / skip

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/now")
    async def now():
        """Untyped on purpose — bridges as a raw JSON field.

        The startup notice flags it: no typed response contract, so the
        schema cannot promise fields. Annotate (or set response_model)
        for a structured, field-selectable type.
        """
        return {"now": datetime.now(timezone.utc).isoformat()}

    # ------------------------------------------- bridging behavior showcases

    @app.get(
        "/products/{product_id}/tag",
        response_model=ProductTag,
        response_model_exclude_unset=True,
        tags=["shop:catalog"],
    )
    async def product_tag(
        product_id: Annotated[int, Path(description="the product to tag")],
    ) -> ProductTag:
        """Sparse price-tag view — how response filtering is bridged.

        Plain products leave ``promo_code``/``featured`` unset, so the JSON
        lacks those keys. Per-field GraphQL promises cannot survive that, so
        the bridge types this field as a raw ``JSON`` scalar (no field
        selection) and says so in the field description.
        """
        product = app.state.products.get(product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="product not found")
        return ProductTag(
            id=product["id"], name=product["name"], price_cents=product["price_cents"]
        )

    @app.get(
        "/v1/orders",
        response_model=list[OrderOut],
        tags=["shop:orders"],
        deprecated=True,
    )
    async def legacy_orders(
        status: Annotated[
            str | None, Query(description="filter by order status, e.g. paid")
        ] = None,
        _token: str = Depends(verify_token),
    ) -> list[OrderOut]:
        """Legacy order listing — how deprecation is bridged.

        OpenAPI ``deprecated: true`` becomes GraphQL-native ``@deprecated``:
        GraphiQL strikes the field through, default introspection hides it,
        and it stays executable for old clients. Use ``list_orders`` instead.
        """
        orders = app.state.orders.values()
        if status is not None:
            orders = (o for o in orders if o["status"] == status)
        return [OrderOut.model_validate(o) for o in orders]

    @app.post("/products/{product_id}/image", tags=["shop:catalog"])
    async def upload_product_image(
        product_id: Annotated[int, Path(description="the product to illustrate")],
        caption: Annotated[str, Form(description="alt text for the image")],
        image: Annotated[bytes, File(description="image bytes")],
    ) -> dict[str, int]:
        """Upload a product image — how Form/File routes are handled.

        Multipart bodies have no MCP input channel (tool arguments are JSON;
        the protocol-level fix, SEP-2631, is still draft), so the scanner
        skips this route with an explicit logged reason instead of exposing
        a GraphQL field that would always 422. The route itself keeps
        working over plain HTTP.
        """
        return {"product_id": product_id, "size": len(image)}

    # ---------------------------------------------- startup-notice showcase
    #
    # One route per startup notice: together with the routes above, every
    # skip reason and every raw-JSON bridge reason from the README capability
    # table fires at startup — boot the server and read the log lines.

    @app.get("/lookup/{ref}", tags=["shop:catalog"])
    async def lookup(ref: Annotated[str, Path(description="u<id> or p<id>")]) -> (
        UserOut | ProductOut
    ):
        """Polymorphic lookup — how a union RESPONSE is bridged.

        ``UserOut | ProductOut`` promises two shapes for one field, and a
        GraphQL field must pick exactly one type. The bridge keeps the field
        callable as a raw ``JSON`` scalar, and the startup notice names the
        route with the fix (one model per shape).
        """
        try:
            key = int(ref[1:])
        except ValueError:
            raise HTTPException(status_code=404, detail="ref must be u<id> or p<id>")
        if ref[:1] == "u":
            user = app.state.users.get(key)
            if user is None:
                raise HTTPException(status_code=404, detail="no such user")
            return UserOut.model_validate(user)
        if ref[:1] == "p":
            product = app.state.products.get(key)
            if product is None:
                raise HTTPException(status_code=404, detail="no such product")
            return ProductOut.model_validate(product)
        raise HTTPException(status_code=404, detail="ref must be u<id> or p<id>")

    @app.get("/search/{term}", response_model=SearchHit, tags=["shop:catalog"])
    async def search(
        term: Annotated[str, Path(description="case-insensitive name fragment")],
    ) -> SearchHit:
        """First name match across users and products — union FIELD bridge.

        The route's own type is a model, but ``SearchHit.hit`` is a union
        field. The bridge keeps the field callable as a raw ``JSON`` scalar,
        and the startup notice names the field with the fix.
        """
        for u in app.state.users.values():
            if term.lower() in u["name"].lower():
                return SearchHit(hit=UserOut.model_validate(u))
        for p in app.state.products.values():
            if term.lower() in p["name"].lower():
                return SearchHit(hit=ProductOut.model_validate(p))
        raise HTTPException(status_code=404, detail="no match")

    @app.get("/banner.txt")
    async def banner() -> PlainTextResponse:
        """Plain-text banner — a raw ``Response`` return is skipped.

        A ``Response`` return promises bytes on the wire, not a typed body;
        there are no fields to expose. The scanner skips the route with that
        reason; plain HTTP keeps serving it.
        """
        return PlainTextResponse("everything must go — 20% off this week\n")

    @app.get("/telemetry")
    async def telemetry(
        x_client_version: Annotated[str, Header(description="calling client build")],
    ) -> dict[str, str]:
        """Client telemetry sink — a REQUIRED header param is skipped.

        Headers are not GraphQL arguments. A required one would make the
        field uncallable (nothing could supply it), so the scanner skips the
        route. Make headers optional; caller credentials ride
        ``passthrough_headers`` instead.
        """
        return {"client_version": x_client_version}

    @app.get("/internal/metrics", include_in_schema=False)
    async def internal_metrics() -> dict[str, int]:
        """Ops-only metrics — hidden routes are skipped by default.

        ``include_in_schema=False`` means "not part of the public contract";
        the scanner honors it (pass ``include_hidden=True`` to expose).
        """
        return {"orders": len(app.state.orders), "users": len(app.state.users)}

    @app.get("/price-watch")
    async def price_watch(
        filters: Annotated[PriceFilter, Query(description="price-side filters")],
        limit: int = 5,
    ) -> dict[str, int]:
        """Query model mixed with a plain param — skipped.

        FastAPI flattens a LONE query model into individual query keys; mixed
        with plain params it has no wire shape at all (requests 422 asking
        for a literal ``filters`` key). The scanner skips the route with the
        fix: move the plain parameters into the model.
        """
        return {"limit": limit}

    @app.post("/alerts")
    async def create_price_alert(payload: AlertCreate) -> dict[str, int]:
        """Register a price alert — an unsupported INPUT type is skipped.

        ``watched: set[int]`` is fine for pydantic over HTTP (a JSON array of
        product ids), but GraphQL has no set input; guessing ``list`` would
        change semantics, so the scanner skips the route and says so.
        """
        return {"watching": len(payload.watched)}

    return app


app = create_app()  # module-level instance (tests import create_app instead)
