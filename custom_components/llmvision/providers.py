from abc import ABC, abstractmethod
import boto3
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from functools import partial
import logging
import inspect
import re
import json
import base64
from .const import (
    DOMAIN,
    CONF_API_KEY,
    CONF_AZURE_BASE_URL,
    CONF_AZURE_DEPLOYMENT,
    CONF_AZURE_VERSION,
    CONF_CUSTOM_OPENAI_ENDPOINT,
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_AWS_REGION_NAME,
    CONF_MOONDREAM_IMAGE_SELECTION,
    VERSION_ANTHROPIC,
    ENDPOINT_OPENAI,
    ENDPOINT_AZURE,
    ENDPOINT_ANTHROPIC,
    ENDPOINT_GOOGLE,
    ENDPOINT_LOCALAI,
    ENDPOINT_OLLAMA,
    ENDPOINT_OPENWEBUI,
    ENDPOINT_GROQ,
    ENDPOINT_MOONDREAM,
    ERROR_NOT_CONFIGURED,
    ERROR_GROQ_MULTIPLE_IMAGES,
    ERROR_MOONDREAM_MULTIPLE_IMAGES,
    ERROR_NO_IMAGE_INPUT, 
    DEFAULT_OPENAI_MODEL,
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_AZURE_MODEL,
    DEFAULT_GOOGLE_MODEL,
    DEFAULT_GROQ_MODEL,
    DEFAULT_LOCALAI_MODEL,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_CUSTOM_OPENAI_MODEL,
    DEFAULT_AWS_MODEL,
    DEFAULT_OPENWEBUI_MODEL,
    DEFAULT_MOONDREAM_MODEL,
    MOONDREAM_IMAGE_SELECTION_FIRST,
    MOONDREAM_IMAGE_SELECTION_LAST,
    MOONDREAM_IMAGE_SELECTION_BEST,
)

_LOGGER = logging.getLogger(__name__)


class Request:
    def __init__(self, hass, message, max_tokens, temperature):
        self.session = async_get_clientsession(hass)
        self.hass = hass
        self.message = message
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.base64_images = []
        self.filenames = []
        self.ssim_scores = []  # Add SSIM scores for better image selection

    @staticmethod
    def sanitize_data(data):
        """Remove long string data from request data to reduce log size"""
        if isinstance(data, dict):
            return {key: Request.sanitize_data(value) for key, value in data.items()}
        elif isinstance(data, list):
            return [Request.sanitize_data(item) for item in data]
        elif isinstance(data, str) and len(data) > 400 and data.count(' ') < 50:
            return '<long_string>'
        elif isinstance(data, bytes) and len(data) > 400:
            return '<long_bytes>'
        else:
            return data

    @staticmethod
    def get_provider(hass, provider_uid):
        """Translate UID of the config entry into provider name"""
        if DOMAIN not in hass.data:
            return None

        entry_data = hass.data[DOMAIN].get(provider_uid)
        if not entry_data:
            return None

        # Check provider field first
        provider = entry_data.get("provider")
        if provider:
            return provider
            
        # Fallback to checking specific config fields for older versions
        if CONF_API_KEY in entry_data:
            # Check which provider based on other config fields
            if CONF_AZURE_BASE_URL in entry_data:
                return "Azure"
            elif CONF_CUSTOM_OPENAI_ENDPOINT in entry_data:
                return "Custom OpenAI"
            elif CONF_AWS_ACCESS_KEY_ID in entry_data:
                return "AWS Bedrock"
            elif CONF_MOONDREAM_IMAGE_SELECTION in entry_data:
                return "Moondream"
            else:
                return "OpenAI"  # Default fallback
        elif "ip_address" in entry_data:
            if "port" in entry_data:
                return "LocalAI"  # or "Ollama" - would need more logic to distinguish
        
        return None
    
    @staticmethod
    def _get_default_model(provider):
        _LOGGER.debug(f"Returning default model for provider: {provider}")
        if provider == "OpenAI":
            return DEFAULT_OPENAI_MODEL
        elif provider == "Azure":
            return DEFAULT_AZURE_MODEL
        elif provider == "Anthropic":
            return DEFAULT_ANTHROPIC_MODEL
        elif provider == "Google":
            return DEFAULT_GOOGLE_MODEL
        elif provider == "Groq":
            return DEFAULT_GROQ_MODEL
        elif provider == "LocalAI":
            return DEFAULT_LOCALAI_MODEL
        elif provider == "Ollama":
            return DEFAULT_OLLAMA_MODEL
        elif provider == "Custom OpenAI":
            return DEFAULT_CUSTOM_OPENAI_MODEL
        elif provider == "AWS Bedrock":
            return DEFAULT_AWS_MODEL
        elif provider == "OpenWebUI":
            return DEFAULT_OPENWEBUI_MODEL
        elif provider == "Moondream":
            return DEFAULT_MOONDREAM_MODEL
        else:
            return

    def validate(self, call) -> None | ServiceValidationError:
        """Validate call data"""

        # if not call.model set default model for provider
        if not call.model:
            call.model = Request._get_default_model(self.get_provider(self.hass, call.provider))

        _LOGGER.info(f"Using model: {call.model}")

        # Check image input
        if not call.base64_images:
            raise ServiceValidationError(ERROR_NO_IMAGE_INPUT)
        
        provider = self.get_provider(self.hass, call.provider)
        
        # Check if single image is provided for Groq
        if len(call.base64_images) > 1 and provider == 'Groq':
            raise ServiceValidationError(ERROR_GROQ_MULTIPLE_IMAGES)
            
        # Check if multiple images provided for Moondream
        if len(call.base64_images) > 1 and provider == 'Moondream':
            # Don't raise error, just log that we'll select one image
            _LOGGER.info(f"Moondream only supports one image per call. Will select image based on configuration.")
        
        # Check provider is configured
        if not call.provider:
            raise ServiceValidationError(ERROR_NOT_CONFIGURED)

    async def call(self, call):
        """
        Forwards a request to the specified provider and optionally generates a title.

        Args:
            call (object): The call object containing request details.

        Raises:
            ServiceValidationError: If the provider is invalid.

        Returns:
            dict: A dictionary containing the generated title (if any) and the response text.
        """
        entry_id = call.provider
        config = self.hass.data.get(DOMAIN).get(entry_id)

        provider = Request.get_provider(self.hass, entry_id)
        call.base64_images = self.base64_images
        call.filenames = self.filenames
        call.ssim_scores = self.ssim_scores  # Pass SSIM scores to call

        self.validate(call)

        if provider == 'OpenAI':
            api_key = config.get(CONF_API_KEY)
            provider_instance = OpenAI(
                hass=self.hass, api_key=api_key, model=call.model)

        elif provider == 'Azure':
            api_key = config.get(CONF_API_KEY)
            endpoint = config.get(CONF_AZURE_BASE_URL)
            deployment = config.get(CONF_AZURE_DEPLOYMENT)
            version = config.get(CONF_AZURE_VERSION)

            provider_instance = AzureOpenAI(self.hass,
                                            api_key=api_key,
                                            endpoint={
                                                'base_url': ENDPOINT_AZURE,
                                                'endpoint': endpoint,
                                                'deployment': deployment,
                                                'api_version': version
                                            },
                                            model=call.model)

        elif provider == 'Anthropic':
            api_key = config.get(CONF_API_KEY)
            provider_instance = Anthropic(self.hass, api_key=api_key, model=call.model)

        elif provider == 'Google':
            api_key = config.get(CONF_API_KEY)
            provider_instance = Google(self.hass, api_key=api_key, endpoint={
                                       'base_url': ENDPOINT_GOOGLE, 'model': call.model})

        elif provider == 'Groq':
            api_key = config.get(CONF_API_KEY)
            provider_instance = Groq(self.hass, api_key=api_key, model=call.model)

        elif provider == 'LocalAI':
            ip_address = config.get('ip_address')
            port = config.get('port')
            https = config.get('https', False)

            provider_instance = LocalAI(self.hass, endpoint={
                'ip_address': ip_address,
                'port': port,
                'https': https
            },
                model=call.model)

        elif provider == 'Ollama':
            ip_address = config.get('ip_address')
            port = config.get('port')
            https = config.get('https', False)

            provider_instance = Ollama(self.hass, endpoint={
                'ip_address': ip_address,
                'port': port,
                'https': https
            },
            model=call.model)

        elif provider == 'Custom OpenAI':
            api_key = config.get(CONF_API_KEY)
            endpoint = config.get(CONF_CUSTOM_OPENAI_ENDPOINT)
            provider_instance = OpenAI(
                self.hass, api_key=api_key, endpoint={'base_url': endpoint}, model=call.model)

        elif provider == 'AWS Bedrock':
            provider_instance = AWSBedrock(self.hass,
                                           aws_access_key_id=config.get(
                                               CONF_AWS_ACCESS_KEY_ID),
                                           aws_secret_access_key=config.get(
                                               CONF_AWS_SECRET_ACCESS_KEY),
                                           aws_region_name=config.get(
                                               CONF_AWS_REGION_NAME),
                                           model=call.model
                                           )

        elif provider == 'OpenWebUI':
            ip_address = config.get('ip_address')
            port = config.get('port')
            https = config.get('https', False)
            api_key = config.get(CONF_API_KEY)

            endpoint = ENDPOINT_OPENWEBUI.format(
                ip_address=ip_address,
                port=port,
                protocol="https" if https else "http"
            )

            provider_instance = OpenAI(
                self.hass, api_key=api_key, endpoint={'base_url': endpoint}, model=call.model)

        elif provider == 'Moondream':
            api_key = config.get(CONF_API_KEY)
            image_selection = config.get(CONF_MOONDREAM_IMAGE_SELECTION, MOONDREAM_IMAGE_SELECTION_FIRST)
            provider_instance = Moondream(self.hass, api_key=api_key, model=call.model, image_selection=image_selection)

        else:
            raise ServiceValidationError("invalid_provider")

        # Make call to provider
        response_text = await provider_instance.vision_request(call)

        if call.generate_title:
            call.message = call.memory.title_prompt + \
                "Create a title for this text: " + response_text
            gen_title = await provider_instance.title_request(call)

            return {"title": re.sub(r'[^a-zA-Z0-9ŽžÀ-ÿ\s]', '', gen_title), "response_text": response_text}
        else:
            return {"response_text": response_text}

    def add_frame(self, base64_image, filename, ssim_score=0.0):
        self.base64_images.append(base64_image)
        self.filenames.append(filename)
        self.ssim_scores.append(ssim_score)

    async def _resolve_error(self, response, provider):
        """Translate response status to error message"""
        full_response_text = await response.text()
        _LOGGER.info(f"[INFO] Full Response: {full_response_text}")

        try:
            response_json = json.loads(full_response_text)
            if provider == 'anthropic':
                error_info = response_json.get('error', {})
                error_message = f"{error_info.get('type', 'Unknown error')}: {error_info.get('message', 'Unknown error')}"
            elif provider == 'ollama':
                error_message = response_json.get('error', 'Unknown error')
            elif provider == 'moondream':
                error_info = response_json.get('error', {})
                error_message = error_info.get('message', 'Unknown error')
            else:
                error_info = response_json.get('error', {})
                error_message = error_info.get('message', 'Unknown error')
        except json.JSONDecodeError:
            error_message = 'Unknown error'

        return error_message


class Provider(ABC):
    """
    Abstract base class for providers

    Args:
        hass (object): Home Assistant instance
        api_key (str, optional): API key for the provider, defaults to ""
        endpoint (dict, optional): Endpoint configuration for the provider
    """

    def __init__(self,
                 hass,
                 api_key,
                 model,
                 endpoint={
                     'base_url': "",
                     'deployment': "",
                     'api_version': "",
                     'ip_address': "",
                     'port': "",
                     'https': False
                 }
                 ):
        self.hass = hass
        self.session = async_get_clientsession(hass)
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint

    @abstractmethod
    async def _make_request(self, data) -> str:
        pass

    @abstractmethod
    def _prepare_vision_data(self, call) -> dict:
        pass

    @abstractmethod
    def _prepare_text_data(self, call) -> dict:
        pass

    @abstractmethod
    async def validate(self) -> None | ServiceValidationError:
        pass

    async def vision_request(self, call) -> str:
        data = self._prepare_vision_data(call)
        return await self._make_request(data)

    async def title_request(self, call) -> str:
        call.temperature = 0.1
        call.max_tokens = 10
        data = self._prepare_text_data(call)
        return await self._make_request(data)

    async def _post(self, url, headers, data) -> dict:
        """Post data to url and return response data"""
        _LOGGER.info(f"Request data: {Request.sanitize_data(data)}")

        try:
            _LOGGER.info(f"Posting to {url}")
            response = await self.session.post(url, headers=headers, json=data)
        except Exception as e:
            raise ServiceValidationError(f"Request failed: {e}")

        if response.status != 200:
            frame = inspect.stack()[1]
            provider = frame.frame.f_locals["self"].__class__.__name__.lower()
            parsed_response = await self._resolve_error(response, provider)
            raise ServiceValidationError(parsed_response)
        else:
            response_data = await response.json()
            _LOGGER.info(f"Response data: {response_data}")
            return response_data

    async def _resolve_error(self, response, provider) -> str:
        """Translate response status to error message"""
        full_response_text = await response.text()
        _LOGGER.info(f"[INFO] Full Response: {full_response_text}")

        try:
            response_json = json.loads(full_response_text)
            if provider == 'anthropic':
                error_info = response_json.get('error', {})
                error_message = f"{error_info.get('type', 'Unknown error')}: {error_info.get('message', 'Unknown error')}"
            elif provider == 'ollama':
                error_message = response_json.get('error', 'Unknown error')
            elif provider == 'moondream':
                error_info = response_json.get('error', {})
                error_message = error_info.get('message', 'Unknown error')
            else:
                error_info = response_json.get('error', {})
                error_message = error_info.get('message', 'Unknown error')
        except json.JSONDecodeError:
            error_message = 'Unknown error'

        return error_message


class Moondream(Provider):
    def __init__(self, hass, api_key, model=DEFAULT_MOONDREAM_MODEL, image_selection=MOONDREAM_IMAGE_SELECTION_FIRST):
        super().__init__(hass, api_key, model)
        self.image_selection = image_selection

    def _generate_headers(self) -> dict:
        return {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}'
        }

    def _select_image(self, call):
        """Select which image to send based on configuration"""
        if len(call.base64_images) == 1:
            return call.base64_images[0], call.filenames[0] if call.filenames else ""
        
        if self.image_selection == MOONDREAM_IMAGE_SELECTION_FIRST:
            return call.base64_images[0], call.filenames[0] if call.filenames else ""
        elif self.image_selection == MOONDREAM_IMAGE_SELECTION_LAST:
            return call.base64_images[-1], call.filenames[-1] if call.filenames else ""
        elif self.image_selection == MOONDREAM_IMAGE_SELECTION_BEST:
            # For "best" image, find the one with lowest SSIM score (most different/interesting)
            if hasattr(call, 'ssim_scores') and call.ssim_scores:
                best_index = call.ssim_scores.index(min(call.ssim_scores))
                return call.base64_images[best_index], call.filenames[best_index] if call.filenames else ""
            else:
                # Fallback to first image if no SSIM scores available
                _LOGGER.warning("SSIM scores not available for best image selection, using first image")
                return call.base64_images[0], call.filenames[0] if call.filenames else ""
        else:
            return call.base64_images[0], call.filenames[0] if call.filenames else ""

    async def _make_request(self, data) -> str:
        headers = self._generate_headers()
        response = await self._post(url=ENDPOINT_MOONDREAM, headers=headers, data=data)
        response_text = response.get("answer", "")
        return response_text

    def _prepare_vision_data(self, call) -> dict:
        # Select single image based on configuration
        selected_image, selected_filename = self._select_image(call)
        
        # Moondream expects the image as a data URI
        image_url = f"data:image/jpeg;base64,{selected_image}"
        
        payload = {
            "image_url": image_url,
            "question": call.message,
            "stream": False
        }

        if call.use_memory and hasattr(call, 'memory'):
            # Add system prompt to the question if memory is used
            system_prompt = call.memory.system_prompt
            if system_prompt:
                payload["question"] = f"{system_prompt}\n\n{call.message}"

        return payload

    def _prepare_text_data(self, call) -> dict:
        # For text-only requests (like title generation), we still need an image
        # Use the first available image or a placeholder
        if call.base64_images:
            selected_image, _ = self._select_image(call)
            image_url = f"data:image/jpeg;base64,{selected_image}"
        else:
            # This shouldn't happen for Moondream, but just in case
            raise ServiceValidationError("Moondream requires an image for all requests")
        
        return {
            "image_url": image_url,
            "question": call.message,
            "stream": False
        }

    async def validate(self) -> None | ServiceValidationError:
        if not self.api_key:
            raise ServiceValidationError("empty_api_key")

        headers = self._generate_headers()
        # Simple validation payload - create a small test image
        test_image = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        data = {
            "image_url": f"data:image/png;base64,{test_image}",
            "question": "Test",
            "stream": False
        }
        await self._post(url=ENDPOINT_MOONDREAM, headers=headers, data=data)


class OpenAI(Provider):
    def __init__(self, hass, api_key, model, endpoint={'base_url': ENDPOINT_OPENAI}):
        super().__init__(hass, api_key, model, endpoint=endpoint)

    def _generate_headers(self) -> dict:
        return {'Content-type': 'application/json',
                'Authorization': 'Bearer ' + self.api_key}

    async def _make_request(self, data) -> str:
        headers = self._generate_headers()
        if isinstance(self.endpoint, dict):
            url = self.endpoint.get('base_url')
        else:
            url = self.endpoint
        response = await self._post(url=url, headers=headers, data=data)
        response_text = response.get(
            "choices")[0].get("message").get("content")
        return response_text

    def _prepare_vision_data(self, call) -> list:
        payload = {"model": self.model,
                   "messages": [{"role": "user", "content": []}],
                   "max_tokens": call.max_tokens,
                   "temperature": call.temperature
                   }

        for image, filename in zip(call.base64_images, call.filenames):
            tag = ("Image " + str(call.base64_images.index(image) + 1)
                   ) if filename == "" else filename
            payload["messages"][0]["content"].append(
                {"type": "text", "text": tag + ":"})
            payload["messages"][0]["content"].append({"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{image}"}})

        payload["messages"][0]["content"].append(
            {"type": "text", "text": call.message})

        if call.use_memory:
            memory_content = call.memory._get_memory_images(
                memory_type="OpenAI")
            system_prompt = call.memory.system_prompt
            if memory_content:
                payload["messages"].insert(
                    0, {"role": "user", "content": memory_content})
            if system_prompt:
                payload["messages"].insert(
                    0, {"role": "developer", "content": system_prompt})

        return payload

    def _prepare_text_data(self, call) -> list:
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": [{"type": "text", "text": call.message}]}],
            "max_tokens": call.max_tokens,
            "temperature": call.temperature
        }

    async def validate(self) -> None | ServiceValidationError:
        if self.api_key:
            headers = self._generate_headers()
            data = {
                "model": self.model,
                "messages": [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}],
                "max_tokens": 1,
                "temperature": 0.5
            }
            await self._post(url=self.endpoint.get('base_url'), headers=headers, data=data)
        else:
            raise ServiceValidationError("empty_api_key")


class AzureOpenAI(Provider):
    def __init__(self, hass, api_key, model, endpoint={'base_url': ENDPOINT_AZURE, 'endpoint': "", 'deployment': "", 'api_version': ""}):
        super().__init__(hass, api_key, model, endpoint)

    def _generate_headers(self) -> dict:
        return {'Content-type': 'application/json',
                'api-key': self.api_key}

    async def _make_request(self, data) -> str:
        headers = self._generate_headers()
        endpoint = self.endpoint.get("base_url").format(
            base_url=self.endpoint.get("endpoint"),
            deployment=self.endpoint.get("deployment"),
            api_version=self.endpoint.get("api_version")
        )

        response = await self._post(url=endpoint, headers=headers, data=data)
        response_text = response.get(
            "choices")[0].get("message").get("content")
        return response_text

    def _prepare_vision_data(self, call) -> list:
        payload = {"messages": [{"role": "user", "content": []}],
                   "max_tokens": call.max_tokens,
                   "temperature": call.temperature,
                   "stream": False
                   }
        for image, filename in zip(call.base64_images, call.filenames):
            tag = ("Image " + str(call.base64_images.index(image) + 1)
                   ) if filename == "" else filename
            payload["messages"][0]["content"].append(
                {"type": "text", "text": tag + ":"})
            payload["messages"][0]["content"].append({"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{image}"}})
        payload["messages"][0]["content"].append(
            {"type": "text", "text": call.message})

        if call.use_memory:
            memory_content = call.memory._get_memory_images(
                memory_type="OpenAI")
            system_prompt = call.memory.system_prompt
            if memory_content:
                payload["messages"].insert(
                    0, {"role": "user", "content": memory_content})
            if system_prompt:
                payload["messages"].insert(
                    0, {"role": "developer", "content": system_prompt})
        return payload

    def _prepare_text_data(self, call) -> list:
        return {"messages": [{"role": "user", "content": [{"type": "text", "text": call.message}]}],
                "max_tokens": call.max_tokens,
                "temperature": call.temperature,
                "stream": False
                }

    async def validate(self) -> None | ServiceValidationError:
        if not self.api_key:
            raise ServiceValidationError("empty_api_key")

        endpoint = self.endpoint.get("base_url").format(
            base_url=self.endpoint.get("endpoint"),
            deployment=self.endpoint.get("deployment"),
            api_version=self.endpoint.get("api_version")
        )
        headers = self._generate_headers()
        data = {"messages": [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}],
                "max_tokens": 1,
                "temperature": 0.5,
                "stream": False
                }
        await self._post(url=endpoint, headers=headers, data=data)


class Anthropic(Provider):
    def __init__(self, hass, api_key, model):
        super().__init__(hass, api_key, model)

    def _generate_headers(self) -> dict:
        return {
            'content-type': 'application/json',
            'x-api-key': self.api_key,
            'anthropic-version': VERSION_ANTHROPIC
        }

    async def _make_request(self, data) -> str:
        headers = self._generate_headers()
        response = await self._post(url=ENDPOINT_ANTHROPIC, headers=headers, data=data)
        response_text = response.get("content")[0].get("text")
        return response_text

    def _prepare_vision_data(self, call) -> dict:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": []}],
            "max_tokens": call.max_tokens,
            "temperature": call.temperature
        }
        for image, filename in zip(call.base64_images, call.filenames):
            tag = ("Image " + str(call.base64_images.index(image) + 1)
                   ) if filename == "" else filename
            payload["messages"][0]["content"].append(
                {"type": "text", "text": tag + ":"})
            payload["messages"][0]["content"].append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": f"{image}"}})
        payload["messages"][0]["content"].append(
            {"type": "text", "text": call.message})

        if call.use_memory:
            memory_content = call.memory._get_memory_images(
                memory_type="Anthropic")
            system_prompt = call.memory.system_prompt
            if memory_content:
                payload["messages"].insert(
                    0, {"role": "user", "content": memory_content})
            if system_prompt:
                payload["system"] = system_prompt

        return payload

    def _prepare_text_data(self, call) -> dict:
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": [{"type": "text", "text": call.message}]}],
            "max_tokens": call.max_tokens,
            "temperature": call.temperature
        }

    async def validate(self) -> None | ServiceValidationError:
        if not self.api_key:
            raise ServiceValidationError("empty_api_key")

        header = self._generate_headers()
        payload = {
            "model": "claude-3-haiku-20240307",
            "messages": [
                  {"role": "user", "content": "Hi"}
            ],
            "max_tokens": 1,
            "temperature": 0.5
        }
        await self._post(url=f"https://api.anthropic.com/v1/messages", headers=header, data=payload)


class Google(Provider):
    def __init__(self, hass, api_key="", endpoint={'base_url': ENDPOINT_GOOGLE, 'model': DEFAULT_GOOGLE_MODEL}):
        super().__init__(hass, api_key, endpoint)
        self.default_model = endpoint['model']

    def _generate_headers(self) -> dict:
        return {'content-type': 'application/json'}

    async def _make_request(self, data) -> str:
        try:
            endpoint = self.endpoint.get('base_url').format(
            model=self.endpoint.get('model'), api_key=self.api_key)

            headers = self._generate_headers()
            response = await self._post(url=endpoint, headers=headers, data=data)
            response_text = response.get("candidates")[0].get(
                "content").get("parts")[0].get("text")
        except Exception as e:
            _LOGGER.error(f"Error: {e}")
            return "Event Detected" # this would still make the automation succeed, but the user will see an error in log, and event calendar will show the event has no further summary.
        return response_text

    def _prepare_vision_data(self, call) -> dict:
        payload = {"contents": [{"role": "user", "parts": []}], "generationConfig": {
            "maxOutputTokens": call.max_tokens, "temperature": call.temperature}}
        for image, filename in zip(call.base64_images, call.filenames):
            tag = ("Image " + str(call.base64_images.index(image) + 1)
                   ) if filename == "" else filename
            payload["contents"][0]["parts"].append({"text": tag + ":"})
            payload["contents"][0]["parts"].append(
                {"inline_data": {"mime_type": "image/jpeg", "data": image}})
        payload["contents"][0]["parts"].append({"text": call.message})

        if call.use_memory:
            memory_content = call.memory._get_memory_images(
                memory_type="Google")
            system_prompt = call.memory.system_prompt
            if memory_content:
                payload["contents"].insert(
                    0, {"role": "user", "parts": memory_content})
            if system_prompt:
                payload["system_instruction"] = {
                    "parts": {"text": system_prompt}}

        return payload

    def _prepare_text_data(self, call) -> dict:
        return {
            "contents": [{"role": "user", "parts": [{"text": call.message + ":"}]}],
            "generationConfig": {"maxOutputTokens": call.max_tokens, "temperature": call.temperature}
        }

    async def validate(self) -> None | ServiceValidationError:
        if not self.api_key:
            raise ServiceValidationError("empty_api_key")

        headers = self._generate_headers()
        data = {
            "contents": [{"role": "user", "parts": [{"text": "Hi"}]}],
            "generationConfig": {"maxOutputTokens": 1, "temperature": 0.5}
        }
        await self._post(url=self.endpoint.get('base_url').format(model=self.endpoint.get('model'), api_key=self.api_key), headers=headers, data=data)


class Groq(Provider):
    def __init__(self, hass, api_key, model):
        super().__init__(hass, api_key, model)

    def _generate_headers(self) -> dict:
        return {'Content-type': 'application/json', 'Authorization': 'Bearer ' + self.api_key}

    async def _make_request(self, data) -> str:
        headers = self._generate_headers()
        response = await self._post(url=ENDPOINT_GROQ, headers=headers, data=data)
        response_text = response.get(
            "choices")[0].get("message").get("content")
        return response_text

    def _prepare_vision_data(self, call) -> dict:
        first_image = call.base64_images[0]
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": call.message},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/jpeg;base64,{first_image}"}}
                    ]
                }
            ],
            "model": self.model
        }

        system_prompt = call.memory.system_prompt
        payload["messages"].insert(
            0, {"role": "user", "content": "System Prompt:" + system_prompt})

        return payload

    def _prepare_text_data(self, call) -> dict:
        return {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": call.message}
                    ]
                }
            ],
            "model": self.model
        }

    async def validate(self) -> None | ServiceValidationError:
        if not self.api_key:
            raise ServiceValidationError("empty_api_key")
        headers = self._generate_headers()
        data = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": "Hi"
            }]
        }
        await self._post(url=ENDPOINT_GROQ, headers=headers, data=data)


class LocalAI(Provider):
    def __init__(self, hass, api_key="", model="", endpoint={'ip_address': "", 'port': "", 'https': False}):
        super().__init__(hass, api_key, model, endpoint)

    async def _make_request(self, data) -> str:
        endpoint = ENDPOINT_LOCALAI.format(
            protocol="https" if self.endpoint.get("https") else "http",
            ip_address=self.endpoint.get("ip_address"),
            port=self.endpoint.get("port")
        )

        headers = {}
        response = await self._post(url=endpoint, headers=headers, data=data)
        response_text = response.get(
            "choices")[0].get("message").get("content")
        return response_text

    def _prepare_vision_data(self, call) -> dict:
        payload = {"model": self.model, "messages": [{"role": "user", "content": [
        ]}], "max_tokens": call.max_tokens, "temperature": call.temperature}
        for image, filename in zip(call.base64_images, call.filenames):
            tag = ("Image " + str(call.base64_images.index(image) + 1)
                   ) if filename == "" else filename
            payload["messages"][0]["content"].append(
                {"type": "text", "text": tag + ":"})
            payload["messages"][0]["content"].append(
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}})
        payload["messages"][0]["content"].append(
            {"type": "text", "text": call.message})

        if call.use_memory:
            memory_content = call.memory._get_memory_images(
                memory_type="OpenAI")
            system_prompt = call.memory.system_prompt
            if memory_content:
                payload["messages"].insert(
                    0, {"role": "user", "content": memory_content})
            if system_prompt:
                payload["messages"].insert(
                    0, {"role": "system", "content": system_prompt})

        return payload

    def _prepare_text_data(self, call) -> dict:
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": [{"type": "text", "text": call.message}]}],
            "max_tokens": call.max_tokens,
            "temperature": call.temperature
        }

    async def validate(self) -> None | ServiceValidationError:
        if not self.endpoint.get("ip_address") or not self.endpoint.get("port"):
            raise ServiceValidationError('handshake_failed')
        session = async_get_clientsession(self.hass)
        ip_address = self.endpoint.get("ip_address")
        port = self.endpoint.get("port")
        protocol = "https" if self.endpoint.get("https") else "http"

        try:
            response = await session.get(f"{protocol}://{ip_address}:{port}/readyz")
            if response.status != 200:
                raise ServiceValidationError('handshake_failed')
        except Exception:
            raise ServiceValidationError('handshake_failed')


class Ollama(Provider):
    def __init__(self, hass, api_key="", model="", endpoint={'ip_address': "0.0.0.0", 'port': "11434", 'https': False}):
        super().__init__(hass, api_key, model, endpoint)

    async def _make_request(self, data) -> str:
        https = self.endpoint.get("https")
        ip_address = self.endpoint.get("ip_address")
        port = self.endpoint.get("port")
        protocol = "https" if https else "http"
        endpoint = ENDPOINT_OLLAMA.format(
            ip_address=ip_address,
            port=port,
            protocol=protocol
        )

        response = await self._post(url=endpoint, headers={}, data=data)
        response_text = response.get("message").get("content")
        return response_text

    def _prepare_vision_data(self, call) -> dict:
        payload = {"model": self.model, "messages": [], "stream": False, "options": {
            "num_predict": call.max_tokens, "temperature": call.temperature}}

        if call.use_memory:
            memory_content = call.memory._get_memory_images(
                memory_type="Ollama")
            system_prompt = call.memory.system_prompt
            if memory_content:
                payload["messages"].extend(memory_content)
            if system_prompt:
                payload["system"] = system_prompt

        for image, filename in zip(call.base64_images, call.filenames):
            tag = ("Image " + str(call.base64_images.index(image) + 1)
                   ) if filename == "" else filename
            image_message = {"role": "user",
                             "content": tag + ":", "images": [image]}
            payload["messages"].append(image_message)
        prompt_message = {"role": "user", "content": call.message}
        payload["messages"].append(prompt_message)

        return payload

    def _prepare_text_data(self, call) -> dict:
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": call.message}],
            "stream": False,
            "options": {"num_predict": call.max_tokens, "temperature": call.temperature}
        }

    async def validate(self) -> None | ServiceValidationError:
        if not self.endpoint.get("ip_address") or not self.endpoint.get("port"):
            raise ServiceValidationError('handshake_failed')
        session = async_get_clientsession(self.hass)
        ip_address = self.endpoint.get("ip_address")
        port = self.endpoint.get("port")
        protocol = "https" if self.endpoint.get("https") else "http"

        try:
            _LOGGER.info(
                f"Checking connection to {protocol}://{ip_address}:{port}")
            response = await session.get(f"{protocol}://{ip_address}:{port}/api/tags", headers={})
            _LOGGER.info(f"Response: {response}")
            if response.status != 200:
                raise ServiceValidationError('handshake_failed')
        except Exception as e:
            _LOGGER.error(f"Error: {e}")
            raise ServiceValidationError('handshake_failed')


class AWSBedrock(Provider):
    def __init__(self, hass, aws_access_key_id, aws_secret_access_key, aws_region_name, model):
        super().__init__(hass, "", model)
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.aws_region = aws_region_name

    def _generate_headers(self) -> dict:
        return {'Content-type': 'application/json',
                'Authorization': 'Bearer ' + self.api_key}

    async def _make_request(self, data) -> str:
        response = await self.invoke_bedrock(model=self.model, data=data)
        response_text = response.get("message").get("content")[0].get("text")
        return response_text

    async def invoke_bedrock(self, model, data) -> dict:
        """Post data to url and return response data"""
        _LOGGER.debug(
            f"AWS Bedrock request data: {Request.sanitize_data(data)}")

        try:
            _LOGGER.info(
                f"Invoking Bedrock model {model} in {self.aws_region}")
            client = await self.hass.async_add_executor_job(
                partial(
                    boto3.client,
                    "bedrock-runtime",
                    region_name=self.aws_region,
                    aws_access_key_id=self.aws_access_key_id,
                    aws_secret_access_key=self.aws_secret_access_key
                )
            )

            # Invoke the model with the response stream
            response = await self.hass.async_add_executor_job(
                partial(
                    client.converse,
                    modelId=model,
                    messages=data.get("messages"),
                    inferenceConfig=data.get("inferenceConfig")
                ))
            _LOGGER.debug(f"AWS Bedrock call Response: {response}")

        except Exception as e:
            raise ServiceValidationError(f"Request failed: {e}")

        if response["ResponseMetadata"]["HTTPStatusCode"] != 200:
            frame = inspect.stack()[1]
            provider = frame.frame.f_locals["self"].__class__.__name__.lower()
            parsed_response = await self._resolve_error(response, provider)
            raise ServiceValidationError(parsed_response)
        else:
            # get observability data
            metrics = response.get("metrics")
            latency = metrics.get("latencyMs")
            token_usage = response.get("usage")
            tokens_in = token_usage.get("inputTokens")
            tokens_out = token_usage.get("outputTokens")
            tokens_total = token_usage.get("totalTokens")
            _LOGGER.info(
                f"AWS Bedrock call latency: {latency}ms inputTokens: {tokens_in} outputTokens: {tokens_out} totalTokens: {tokens_total}")
            response_data = response.get("output")
            _LOGGER.debug(f"AWS Bedrock call response data: {response_data}")
            return response_data

    def _prepare_vision_data(self, call) -> list:
        _LOGGER.debug(f"Found model type `{self.model}` for AWS Bedrock call.")
        # We need to generate the correct format for the respective models
        payload = {
            "messages": [{"role": "user", "content": []}],
            "inferenceConfig": {
                "maxTokens": call.max_tokens,
                "temperature": call.temperature
            }
        }

        # Bedrock converse API wants the raw bytes of the image
        for image, filename in zip(call.base64_images, call.filenames):
            tag = ("Image " + str(call.base64_images.index(image) + 1)
                   ) if filename == "" else filename
            payload["messages"][0]["content"].append(
                {"text": tag + ":"})
            payload["messages"][0]["content"].append({
                "image": {
                    "format": "jpeg",
                    "source": {"bytes": base64.b64decode(image)}
                }
            })
        payload["messages"][0]["content"].append({"text": call.message})

        if call.use_memory:
            memory_content = call.memory._get_memory_images(memory_type="AWS")
            system_prompt = call.memory.system_prompt
            if memory_content:
                payload["messages"].insert(
                    0, {"role": "user", "content": memory_content})
            if system_prompt:
                payload["messages"].insert(
                    0, {"role": "user", "content": [{"text": system_prompt}]})

        return payload

    def _prepare_text_data(self, call) -> list:
        return {
            "messages": [{"role": "user", "content": [{"text": call.message}]}],
            "inferenceConfig": {
                "maxTokens": call.max_tokens,
                "temperature": call.temperature
            }
        }

    async def validate(self) -> None | ServiceValidationError:
        data = {
            "messages": [{"role": "user", "content": [{"text": "Hi"}]}],
            "inferenceConfig": {"maxTokens": 10, "temperature": 0.5}
        }
        await self.invoke_bedrock(model=self.model, data=data)
