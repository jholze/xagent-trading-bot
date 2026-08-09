# x_account_analyzer.py
import logging
from datetime import datetime
from typing import Dict, List, Optional

import tweepy

# Logging konfigurieren
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class XAccountAnalyzer:
    """
    Moderne Klasse zur Analyse von X (Twitter) Accounts.
    Unterstützt sowohl v1.1 als auch teilweise v2 Endpoints.
    """

    def __init__(
        self,
        consumer_key: str,
        consumer_secret: str,
        access_token: str,
        access_token_secret: str,
    ) -> None:
        try:
            auth = tweepy.OAuth1UserHandler(
                consumer_key, consumer_secret, access_token, access_token_secret
            )
            self.api_v1 = tweepy.API(auth, wait_on_rate_limit=True)

            # Credentials prüfen
            self.api_v1.verify_credentials()
            logger.info("✅ Erfolgreich mit Twitter API v1.1 verbunden")

        except tweepy.TweepyException as e:
            logger.error(f"❌ Authentifizierung fehlgeschlagen: {e}")
            raise

    def get_account_info(self, username: str) -> Dict:
        """Holt grundlegende Account-Informationen"""
        try:
            user = self.api_v1.get_user(screen_name=username)
            info = {
                "username": user.screen_name,
                "name": user.name,
                "followers": user.followers_count,
                "following": user.friends_count,
                "tweets": user.statuses_count,
                "likes": user.favourites_count,
                "created_at": user.created_at,
                "verified": user.verified,
                "description": user.description,
                "location": user.location,
            }
            logger.info(f"Account-Info für @{username} erfolgreich abgerufen")
            return info
        except tweepy.TweepyException as e:
            logger.error(f"Fehler bei Account-Info @{username}: {e}")
            return {}

    def get_recent_tweets(self, username: str, count: int = 20) -> List:
        """Holt die letzten Tweets eines Users"""
        try:
            tweets = self.api_v1.user_timeline(
                screen_name=username,
                count=count,
                tweet_mode="extended",
                include_rts=False,
            )
            return tweets
        except tweepy.TweepyException as e:
            logger.error(f"Fehler beim Abrufen der Tweets von @{username}: {e}")
            return []

    def analyze_engagement(self, username: str, num_tweets: int = 15) -> Dict:
        """
        Detaillierte Engagement-Analyse
        """
        tweets = self.get_recent_tweets(username, num_tweets)
        if not tweets:
            return {"average_engagement": 0.0, "status": "no_tweets"}

        account = self.get_account_info(username)
        followers = account.get("followers", 1)

        total_likes = 0
        total_retweets = 0
        total_replies = 0
        engagement_rates = []

        for tweet in tweets:
            likes = tweet.favorite_count
            retweets = tweet.retweet_count
            replies = tweet.reply_count if hasattr(tweet, "reply_count") else 0

            total_likes += likes
            total_retweets += retweets
            total_replies += replies

            # Engagement Rate pro Tweet
            engagement = (likes + retweets + replies) / followers
            engagement_rates.append(engagement)

        avg_engagement = sum(engagement_rates) / len(engagement_rates)

        return {
            "average_engagement_rate": round(avg_engagement, 5),
            "total_tweets_analyzed": len(tweets),
            "avg_likes": total_likes // len(tweets),
            "avg_retweets": total_retweets // len(tweets),
            "avg_replies": total_replies // len(tweets),
            "followers": followers,
        }


# ====================== Beispielnutzung ======================
if __name__ == "__main__":
    # Ersetze mit deinen echten Keys
    analyzer = XAccountAnalyzer(
        consumer_key="DEIN_CONSUMER_KEY",
        consumer_secret="DEIN_CONSUMER_SECRET",
        access_token="DEIN_ACCESS_TOKEN",
        access_token_secret="DEIN_ACCESS_TOKEN_SECRET",
    )

    username = "PlayAriaGame"  # Beispiel: AriaAI offizieller Account

    info = analyzer.get_account_info(username)
    print("\n=== Account Info ===")
    for key, value in info.items():
        print(f"{key:15}: {value}")

    engagement = analyzer.analyze_engagement(username, num_tweets=20)
    print("\n=== Engagement Analyse ===")
    for key, value in engagement.items():
        print(f"{key:25}: {value}")
