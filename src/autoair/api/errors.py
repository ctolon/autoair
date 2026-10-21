"""HTTP responses for shared persistence conflicts."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from autoair.persistence.documents import DocumentConflict


def install_document_errors(app: FastAPI) -> None:
    async def conflict(_request: Request, _exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "This record changed in another request. Reload it before retrying."
            },
        )

    app.add_exception_handler(DocumentConflict, conflict)
