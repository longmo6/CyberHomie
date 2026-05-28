import httpx
from utils.logger import setup_logger

logger = setup_logger("napcat")


class NapCatAPIClient:
    def __init__(self, base_url: str, access_token: str = ""):
        headers = {}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        self.client = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=30.0
        )

    async def send_group_message(
        self, group_id: int, message: str, reply_to: int = 0
    ) -> dict:
        """Send group message. If reply_to is set, quote that message."""
        if reply_to:
            msg = [
                {"type": "reply", "data": {"id": str(reply_to)}},
                {"type": "text", "data": {"text": message}},
            ]
        else:
            msg = message
        resp = await self.client.post(
            "/send_group_msg",
            json={"group_id": group_id, "message": msg},
        )
        resp.raise_for_status()
        return resp.json()

    async def send_private_message(self, user_id: int, message: str) -> dict:
        resp = await self.client.post(
            "/send_private_msg",
            json={"user_id": user_id, "message": message},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_group_info(self, group_id: int) -> dict:
        resp = await self.client.post(
            "/get_group_info", json={"group_id": group_id}
        )
        resp.raise_for_status()
        return resp.json()

    async def get_group_member_list(self, group_id: int) -> dict:
        resp = await self.client.post(
            "/get_group_member_list", json={"group_id": group_id}
        )
        resp.raise_for_status()
        return resp.json()

    async def get_group_member_info(
        self, group_id: int, user_id: int
    ) -> dict:
        resp = await self.client.post(
            "/get_group_member_info",
            json={"group_id": group_id, "user_id": user_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def close(self):
        await self.client.aclose()
