# War Thunder Discord Bot

This bot is a personal project of mine based around the game "War Thunder". It is integrated into a Discord server with just over 1000 members who also play the game.

## Features

- Scrape data from War Thunder's squadron webpage to return player name, personal squadron rating and battle activity.
- Allow players to select the vehicles they have for a specific "battle rating", entries then saved in PostgreSQL database for later reference, list of vehicles is pulled from the same database.
- Send a message into a specified Discord channel when user joins a voice chat, if no vehicles are present for the current battle rating a ping will notify the user that they need to enter their vehicles in the database.

## Features in Development

- Return scraped data through the front end buttons, Top 5 players with most points, players who have more than 850 personal squadron rating etc.
- Automatic scraping of game data to track wins/losses and player vehicle type statistics.
- "Man management" features to be built in for Admin work.
- Quality of life additions for members.
- Automatic role management based on War Thunder points. eg 0 points role removed after player has > 0 points.

## More Features Coming Soon

Stay tuned for additional functionality and improvements!
