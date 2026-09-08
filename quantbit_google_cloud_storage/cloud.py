import os
import mimetypes
from urllib.parse import unquote, urlparse

import boto3
import requests
import frappe
from frappe import _
from botocore.exceptions import ClientError
from frappe.utils import get_site_path

# ==========================================
# S3 / GCS CLIENT CONFIGURATION HELPERS
# ==========================================

def get_s3_config():
	"""
	Returns S3 configuration from site config.
	Expects 'file_system_storage' key in site_config.json.
	"""
	config = frappe.conf.get("file_system_storage")
	if not config:
		frappe.throw(_("File System Storage configuration not found in site_config.json"))
	return config

def get_s3_client():
	"""
	Returns a boto3 S3 client using credentials from site config.
	"""
	config = get_s3_config()
	if not config.get("enabled"):
		return None
		
	return boto3.client(
		"s3",
		aws_access_key_id=config.get("access_key"),
		aws_secret_access_key=config.get("secret_key"),
		endpoint_url=config.get("endpoint_url"),
		region_name=config.get("region") or "auto",
	)

def get_bucket_name():
	config = get_s3_config()
	return config.get("bucket_name")


# ==========================================
# CORE STOCK HOOK REPLACEMENTS (WRITE / DELETE)
# ==========================================

def upload_file_to_gcs(*args, **kwargs):
	"""
	Uploads a file to Google Cloud Storage (via S3 API).
	Hook for: write_file
	Handles two signatures:
	1. (file_doc) - called from File.save_file
	2. (fname, content, content_type, is_private) - called from file_manager.save_file
	"""
	fname = None
	content = None
	content_type = None
	is_private = 0
	attached_to_doctype = None
	attached_to_name = None
	
	if len(args) == 1 and hasattr(args[0], "doctype") and args[0].doctype == "File":
		# Case 1: Called with File document
		file_doc = args[0]
		fname = file_doc.file_name
		content = file_doc.get_content()
		content_type = file_doc.file_type
		is_private = file_doc.is_private
		attached_to_doctype = file_doc.attached_to_doctype
		attached_to_name = file_doc.attached_to_name
	elif len(args) >= 2:
		# Case 2: Called with individual arguments
		fname = args[0]
		content = args[1]
		content_type = args[2] if len(args) > 2 else kwargs.get("content_type")
		is_private = args[3] if len(args) > 3 else kwargs.get("is_private", 0)
		attached_to_doctype = frappe.form_dict.get("doctype")
		attached_to_name = frappe.form_dict.get("docname")
	else:
		# Try kwargs
		fname = kwargs.get("fname")
		content = kwargs.get("content")
		content_type = kwargs.get("content_type")
		is_private = kwargs.get("is_private", 0)
		attached_to_doctype = kwargs.get("attached_to_doctype") or frappe.form_dict.get("doctype")
		attached_to_name = kwargs.get("attached_to_name") or frappe.form_dict.get("docname")
		
	if not fname or content is None:
		frappe.throw(_("Missing file name or content for GCS upload"))

	parts = [p.strip().replace(" ", "-") for p in (attached_to_doctype, attached_to_name) if p]
	if parts:
		prefix = "-".join(parts)
		if not fname.startswith(f"{prefix}-"):
			fname = f"{prefix}-{fname}"

	try:
		client = get_s3_client()
		if not client:
			# If disabled, fallback to local filesystem
			from frappe.utils.file_manager import save_file_on_filesystem
			return save_file_on_filesystem(fname, content, content_type, is_private)
		
		s3 = client
		bucket_name = get_bucket_name()
		# Resolve standard lowercase MIME type (e.g., application/pdf)
		mime_type = None
		if fname:
			mime_type, _ = mimetypes.guess_type(fname)
		if not mime_type and content_type:
			if "/" in content_type:
				mime_type = content_type
			else:
				mime_type, _ = mimetypes.guess_type(f"dummy.{content_type.lower()}")
		mime_type = mime_type or "application/octet-stream"
		
		params = {
			"Bucket": bucket_name,
			"Key": fname,
			"Body": content,
			"ContentType": mime_type,
		}
		s3.put_object(**params)
		
		# Construct URL
		config = get_s3_config()
		public_url = config.get("public_dev_url")
		if public_url:
			if not public_url.startswith(("http://", "https://")):
				public_url = f"https://{public_url}"
			if public_url.endswith("/"):
				public_url = public_url[:-1]
			file_url = f"{public_url}/{fname}"
		else:
			endpoint = config.get("endpoint_url")
			if endpoint.endswith("/"):
				endpoint = endpoint[:-1]
			file_url = f"{endpoint}/{bucket_name}/{fname}"

		# Important: If called with a File document, update it in-place!
		# Frappe's File.save_file ignores the return value, so we must update the doc.
		if len(args) == 1 and hasattr(args[0], "doctype") and args[0].doctype == "File":
			file_doc = args[0]
			file_doc.file_name = fname
			file_doc.file_url = file_url
			file_doc.file_size = len(content)

		return {
			"file_name": fname,
			"file_url": file_url
		}

	except Exception as e:
		frappe.log_error("S3 Upload Failed", str(e))
		raise e

def delete_file_from_gcs(doc, only_thumbnail=False):
	"""
	Deletes a file from Google Cloud Storage (via S3 API).
	Hook for: delete_file_data_content
	"""
	try:
		client = get_s3_client()
		if not client:
			from frappe.utils.file_manager import delete_file_from_filesystem
			return delete_file_from_filesystem(doc, only_thumbnail)

		s3 = client
		bucket_name = get_bucket_name()
		if only_thumbnail:
			return

		object_key = None
		for candidate in [getattr(doc, "file_name", None), getattr(doc, "file_url", None)]:
			if not candidate:
				continue

			if "://" in candidate:
				parsed = urlparse(candidate)
				path = unquote(parsed.path.lstrip("/"))
				if path.startswith(f"{bucket_name}/"):
					path = path[len(bucket_name) + 1 :]
				elif path.startswith("files/") or path.startswith("private/files/"):
					path = path.split("/", 1)[1]
				object_key = path
			else:
				object_key = candidate.strip("/")

			if object_key:
				break

			if not object_key:
				frappe.throw(_("Unable to determine object key for remote file deletion"))

		s3.delete_object(Bucket=bucket_name, Key=object_key)

	except Exception as e:
		frappe.log_error("S3 Delete Failed", f"{str(e)} | key={object_key if 'object_key' in locals() else None}")
		raise


# ==========================================
# DATA IMPORT EXCEPTION HANDLING FOR STATELESS INFRAS
# ==========================================

def download_cloud_file(doc, method=None):
	"""
	Triggered via 'before_save' on Data Import DocType.
	Pulls down the remote cloud storage payload onto local storage so 
	native Frappe Importer processing modules can parse it safely.
	"""
	if doc.import_file and (doc.import_file.startswith("http://") or doc.import_file.startswith("https://")):
		try:
			response = requests.get(doc.import_file, timeout=60)
			response.raise_for_status()

			file_name = doc.import_file.split("/")[-1]
			local_dir = get_site_path("public", "files")
			local_path = os.path.join(local_dir, file_name)

			os.makedirs(local_dir, exist_ok=True)
			with open(local_path, "wb") as f:
				f.write(response.content)

			# Store the file path globally on the request document to catch during hook closure
			doc._temporary_local_path = local_path

		except Exception as e:
			frappe.log_error(title="Data Import Cloud Fetch Failed")
			frappe.throw(_("Could not temporarily extract execution file from Cloud Storage: {0}").format(str(e)))

def cleanup_local_file(doc, method=None):
	"""
	Triggered via 'after_save' and 'on_update' on Data Import.
	Safely purges the temporary local file block once the core validation pipeline finishes.
	"""
	local_path = getattr(doc, "_temporary_local_path", None)
	
	if local_path and os.path.exists(local_path):
		try:
			os.remove(local_path)
		except Exception as e:
			frappe.log_error(message=str(e), title="Data Import Local Cleanup Failed")


from frappe.core.doctype.file.file import File

class CustomFileOverride(File):
	def get_content(self):
		"""
		Intercepts content fetching. If the file is stored on GCS/S3, 
		it streams it into memory directly instead of breaking on open().
		"""
		if self.file_url and (self.file_url.startswith("http://") or self.file_url.startswith("https://")):
			try:
				response = requests.get(self.file_url, timeout=60)
				response.raise_for_status()
				return response.content
			except Exception as e:
				frappe.log_error(title="GCS Data Import Stream Failure")
				frappe.throw(_("Could not fetch remote file for processing: {0}").format(str(e)))
		
		# Fallback to stock Frappe behavior for actual local files
		return super().get_content()

def delete_file_after_import(doc, method=None):
	"""
	Triggered on 'on_update' of Data Import.
	Forces deletion of the linked remote file record upon a successful import status.
	"""
	# Frappe might store it as uppercase "Success" or lowercase "success" depending on internal logic
	if doc.status and doc.status.lower() == "success" and doc.import_file:
		
		# 1. Look up the File document matching this specific URL string
		file_data = frappe.db.get_value(
			"File", 
			{"file_url": doc.import_file}, 
			["name", "is_private"], 
			as_dict=True
		)
		
		if file_data:
			try:
				# 2. Bypass standard hook context lock by using a flag, ensuring deletion triggers
				frappe.delete_doc("File", file_data.name, ignore_permissions=True, force=True)
				
				# 3. Clean the import_file variable link so it doesn't try to loop on next doc updates
				frappe.db.set_value("Data Import", doc.name, "import_file", None)
				
				# Commit changes immediately to database to prevent rollback overlap
				frappe.db.commit()
				
			except Exception as e:
				# If it errors out, check your Bench Console or error logs!
				frappe.log_error(
					title="Data Import GCS Auto-Purge Failed",
					message=f"Failed to delete File doc {file_data.name}: {str(e)}"
				)
