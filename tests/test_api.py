"""Tests for the Aigues de Barcelona API client."""

import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

from custom_components.aigues_barcelona.api import AiguesApiClient


class TestAiguesApiClient:
    """Tests for the AiguesApiClient class."""

    @pytest.fixture
    def client(self):
        """Create an API client for testing."""
        return AiguesApiClient(
            username="test_user",
            password="test_password",
            twocaptcha_api_key="test_captcha_key",
            contract="ABC123",
        )

    def test_init(self, client):
        """Test client initialization."""
        assert client._username == "test_user"
        assert client._password == "test_password"
        assert client._twocaptcha_api_key == "test_captcha_key"
        assert client._contract == "ABC123"

    def test_generate_url(self, client):
        """Test URL generation."""
        url = client._generate_url("/test/path", {"key": "value"})
        assert url == "https://api.aiguesdebarcelona.cat/test/path?key=value"

    def test_generate_url_no_query(self, client):
        """Test URL generation without query params."""
        url = client._generate_url("/test/path", None)
        assert url == "https://api.aiguesdebarcelona.cat/test/path"

    def test_generate_url_strips_leading_slash(self, client):
        """Test URL generation strips extra slashes."""
        url = client._generate_url("///test/path", None)
        assert url == "https://api.aiguesdebarcelona.cat/test/path"

    def test_set_token(self, client):
        """Test setting authentication token."""
        client.set_token("test_jwt_token")
        token = client.get_token()
        assert token == "test_jwt_token"

    def test_is_token_expired_no_token(self, client):
        """Test token expiration check with no token."""
        assert client.is_token_expired() is True

    def test_is_token_expired_with_valid_token(self, client):
        """Test token expiration check with valid token."""
        # Create a mock JWT token with future expiration
        import base64
        import json
        
        future_exp = (datetime.datetime.now() + datetime.timedelta(hours=1)).timestamp()
        payload = {"exp": future_exp, "name": "test_user"}
        payload_encoded = base64.urlsafe_b64encode(
            json.dumps(payload).encode()
        ).decode().rstrip("=")
        
        # JWT format: header.payload.signature
        mock_token = f"header.{payload_encoded}.signature"
        client.set_token(mock_token)
        
        assert client.is_token_expired() is False

    def test_is_token_expired_with_expired_token(self, client):
        """Test token expiration check with expired token."""
        import base64
        import json
        
        past_exp = (datetime.datetime.now() - datetime.timedelta(hours=1)).timestamp()
        payload = {"exp": past_exp, "name": "test_user"}
        payload_encoded = base64.urlsafe_b64encode(
            json.dumps(payload).encode()
        ).decode().rstrip("=")
        
        mock_token = f"header.{payload_encoded}.signature"
        client.set_token(mock_token)
        
        assert client.is_token_expired() is True


class TestConsumptions:
    """Tests for consumption-related methods."""

    @pytest.fixture
    def client(self):
        """Create an API client for testing."""
        return AiguesApiClient(
            username="test_user",
            password="test_password",
            twocaptcha_api_key="test_captcha_key",
            contract="ABC123",
        )

    def test_consumptions_invalid_frequency(self, client):
        """Test consumptions method with invalid frequency."""
        with pytest.raises(ValueError, match="Invalid"):
            client.consumptions(
                datetime.date(2026, 1, 10),
                datetime.date(2026, 1, 15),
                contract="ABC123",
                frequency="INVALID",
            )

    def test_parse_consumptions(self, client):
        """Test parsing consumption data."""
        info = [
            {"accumulatedConsumption": 100.0, "other": "data"},
            {"accumulatedConsumption": 101.5, "other": "data"},
            {"accumulatedConsumption": 103.2, "other": "data"},
        ]
        
        result = client.parse_consumptions(info)
        
        assert result == [100.0, 101.5, 103.2]

    def test_parse_consumptions_custom_key(self, client):
        """Test parsing consumption data with custom key."""
        info = [
            {"consumption": 10.0, "accumulatedConsumption": 100.0},
            {"consumption": 11.0, "accumulatedConsumption": 111.0},
        ]
        
        result = client.parse_consumptions(info, key="consumption")
        
        assert result == [10.0, 11.0]


class TestLoginCooldown:
    """Tests for login cooldown functionality."""

    @pytest.fixture
    def client(self):
        """Create an API client for testing."""
        return AiguesApiClient(
            username="test_user",
            password="test_password",
            twocaptcha_api_key="test_captcha_key",
            contract="ABC123",
        )

    def test_login_respects_cooldown(self, client):
        """Test that login respects cooldown period."""
        # Set cooldown in the future
        client._captcha_cooldown_until = datetime.datetime.utcnow() + datetime.timedelta(minutes=5)
        
        with pytest.raises(Exception, match="cooldown"):
            client.login()

    def test_login_allows_after_cooldown(self, client):
        """Test that login is allowed after cooldown expires."""
        # Set cooldown in the past
        client._captcha_cooldown_until = datetime.datetime.utcnow() - datetime.timedelta(minutes=1)
        
        # Should not raise cooldown exception (may raise other exceptions)
        with patch("custom_components.aigues_barcelona.api.TwoCaptcha") as mock_captcha:
            mock_captcha.return_value.recaptcha.return_value = {"code": "test_code"}
            
            with patch.object(client, "_query") as mock_query:
                mock_response = MagicMock()
                mock_response.json.return_value = {"access_token": "test_token"}
                mock_query.return_value = mock_response
                
                result = client.login()
                
                assert result is True

    def test_login_prevents_concurrent_calls(self, client):
        """Test that concurrent login calls are prevented."""
        client._login_in_progress = True
        
        with pytest.raises(Exception, match="Login already in progress"):
            client.login()
