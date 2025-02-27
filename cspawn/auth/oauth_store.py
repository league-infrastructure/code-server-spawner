from flask_dance.consumer.storage import BaseStorage
from flask_login import current_user
from flask import session
from bson.objectid import ObjectId


class MongoDBStorage(BaseStorage):
    def __init__(self, mongo, collection_name="oauth_tokens"):
        """
        Custom MongoDB storage for Flask-Dance OAuth tokens.
        :param mongo: PyMongo instance
        :param collection_name: Name of the MongoDB collection where tokens are stored
        """
        self.mongo = mongo
        self.collection = mongo.db[collection_name]

    def get(self, blueprint):
        """Retrieve the OAuth token for the current user and provider."""
        if not current_user.is_authenticated:
            return session.get(f"{blueprint.name}_oauth_token")

        user = self.collection.find_one({"user_id": str(current_user.id), "provider": blueprint.name})
        return user["token"] if user else None

    def set(self, blueprint, token):
        """Store the OAuth token for the current user and provider."""
        if not current_user.is_authenticated:
            session[f"{blueprint.name}_oauth_token"] = token
            return

        self.collection.update_one(
            {"user_id": str(current_user.id), "provider": blueprint.name},
            {"$set": {"token": token}},
            upsert=True,  # Create if it doesn't exist
        )

    def delete(self, blueprint):
        """Delete the OAuth token for the current user and provider."""
        if not current_user.is_authenticated:
            session.pop(f"{blueprint.name}_oauth_token", None)
            return

        self.collection.delete_one({"user_id": str(current_user.id), "provider": blueprint.name})
