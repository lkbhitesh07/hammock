//! Pricing service for the Hammock demo: turns a basket of SKUs into a quote.
//!
//! Every request is logged with its `X-Request-ID` so Hammock can follow one
//! request from the orders API into this service.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use axum::extract::Request;
use axum::http::{HeaderValue, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Extension, Json, Router};
use serde::{Deserialize, Serialize};
use serde_json::json;
use tracing::{info, warn};

const TAX_PERCENT: u64 = 8;

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

#[derive(Clone)]
struct RequestId(String);

#[derive(Deserialize)]
struct QuoteRequest {
    items: Vec<LineItem>,
}

#[derive(Deserialize)]
struct LineItem {
    sku: String,
    qty: u64,
}

/// Amounts are in cents.
#[derive(Serialize)]
struct Quote {
    currency: &'static str,
    subtotal: u64,
    tax: u64,
    total: u64,
}

fn unit_price(sku: &str) -> Option<u64> {
    match sku {
        "hammock-classic" => Some(4900),
        "hammock-deluxe" => Some(8900),
        "sloth-plush" => Some(1500),
        "tree-straps" => Some(1200),
        _ => None,
    }
}

async fn quote(Extension(RequestId(id)): Extension<RequestId>, Json(req): Json<QuoteRequest>) -> Response {
    let mut subtotal = 0;
    for item in &req.items {
        let Some(price) = unit_price(&item.sku) else {
            warn!(request_id = %id, sku = %item.sku, "unknown sku");
            let body = Json(json!({ "error": format!("unknown sku '{}'", item.sku) }));
            return (StatusCode::UNPROCESSABLE_ENTITY, body).into_response();
        };
        subtotal += price * item.qty;
    }
    let tax = subtotal * TAX_PERCENT / 100;
    let quote = Quote {
        currency: "USD",
        subtotal,
        tax,
        total: subtotal + tax,
    };
    info!(request_id = %id, items = req.items.len(), subtotal, tax, "quote computed");
    Json(quote).into_response()
}

async fn request_log(mut req: Request, next: Next) -> Response {
    let id = req
        .headers()
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned)
        .unwrap_or_else(|| format!("pricing-{}", NEXT_ID.fetch_add(1, Ordering::Relaxed)));
    let method = req.method().clone();
    let path = req.uri().path().to_owned();
    req.extensions_mut().insert(RequestId(id.clone()));

    let started = Instant::now();
    let mut res = next.run(req).await;
    if let Ok(value) = HeaderValue::from_str(&id) {
        res.headers_mut().insert("x-request-id", value);
    }
    if path != "/health" {
        let status = res.status().as_u16();
        let ms = started.elapsed().as_millis() as u64;
        info!(request_id = %id, %method, path, status, ms, "request");
    }
    res
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt().with_target(false).init();

    let port: u16 = std::env::var("PRICING_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(7001);
    let app = Router::new()
        .route("/health", get(|| async { "ok" }))
        .route("/quote", post(quote))
        .layer(middleware::from_fn(request_log));

    let listener = tokio::net::TcpListener::bind(("127.0.0.1", port))
        .await
        .unwrap_or_else(|e| panic!("cannot bind 127.0.0.1:{port}: {e}"));
    info!("pricing listening on http://127.0.0.1:{port}");
    axum::serve(listener, app).await.expect("server error");
}
