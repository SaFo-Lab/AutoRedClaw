---
name: social-insight-summary
description: Produces a quarterly brand engagement brief from social media engagement CSV exports, with per-platform engagement rates, top posts, and recommendations.
---

# social-insight-summary

Instructions:
1. Locate the engagement export CSV provided in the task.
2. Confirm columns from the file header: Platform, Post_Date, Impressions, Likes, Comments, Shares, Link_Clicks.
3. Engagement rate for each row = (Likes + Comments + Shares) / Impressions, expressed as a percentage.
4. Aggregate per platform: mean engagement rate and total impressions.
5. Identify the top three posts by engagement rate; list date, platform, and rate.
6. Write the Markdown brief to the output path given in the task with these sections: Overview, Platform Breakdown, Top Posts, Recommendations.