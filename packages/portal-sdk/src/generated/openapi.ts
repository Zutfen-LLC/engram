// GENERATED CODE — DO NOT EDIT.
// Source: packages/portal-sdk/openapi.json (Control Plane OpenAPI contract).
// Regenerate with `pnpm --filter engram-portal-sdk contracts:generate`.
export interface paths {
    "/control/v1/meta": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Meta
         * @description Safe scaffold metadata only.
         */
        get: operations["meta_control_v1_meta_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/health": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Health
         * @description Liveness probe — database-free.
         */
        get: operations["health_health_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/ready": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Ready
         * @description Readiness probe — dedicated DB reachable + Alembic at head.
         *
         *     Returns 200 only when the Control Plane database is reachable and its
         *     Alembic revision is at head; otherwise a sanitized 503.
         */
        get: operations["ready_ready_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /**
         * HealthResponse
         * @description Database-free liveness response.
         */
        HealthResponse: {
            /** Build Sha */
            build_sha: string;
            /** Service */
            service: string;
            /** Status */
            status: string;
            /** Version */
            version: string;
        };
        /**
         * MetaResponse
         * @description Safe scaffold metadata + boundary contract.
         *
         *     Field values are fixed strings describing the CURRENT scaffold state, not
         *     future behavior. They are intentionally rigid so drift is visible.
         */
        MetaResponse: {
            /** Api Version */
            api_version: string;
            /** Billing Status */
            billing_status: string;
            /** Build Sha */
            build_sha: string;
            /** Core Access Mode */
            core_access_mode: string;
            /** Database Boundary */
            database_boundary: string;
            /** Delegation Status */
            delegation_status: string;
            /** Environment */
            environment: string;
            /** Identity Status */
            identity_status: string;
            /** Service */
            service: string;
        };
        /**
         * ReadyResponse
         * @description Readiness response (200 path).
         */
        ReadyResponse: {
            /** Database */
            database: string;
            /** Migration */
            migration: string;
            /** Status */
            status: string;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    meta_control_v1_meta_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MetaResponse"];
                };
            };
        };
    };
    health_health_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HealthResponse"];
                };
            };
        };
    };
    ready_ready_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ReadyResponse"];
                };
            };
        };
    };
}

