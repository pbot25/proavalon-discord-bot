import asyncio
import os

from dotenv import load_dotenv

from proavalon.profile_scraper import (
    ProAvalonError,
    ProAvalonProfileScraper,
    ProAvalonUserNotFound,
)


load_dotenv()

PROAVALON_USERNAME = os.getenv("PROAVALON_USERNAME")
PROAVALON_PASSWORD = os.getenv("PROAVALON_PASSWORD")

# Use a real, known ProAvalon username.
TEST_PROFILE_USERNAME = "S3Zeraya"


async def main() -> None:
    if not PROAVALON_USERNAME or not PROAVALON_PASSWORD:
        raise RuntimeError(
            "PROAVALON_USERNAME or PROAVALON_PASSWORD "
            "is missing from the .env file."
        )

    scraper = ProAvalonProfileScraper(
        minimum_delay_seconds=3.0,
    )

    try:
        await scraper.start()

        print("Logging into ProAvalon...")

        await scraper.login(
            username=PROAVALON_USERNAME,
            password=PROAVALON_PASSWORD,
        )

        print("Login successful.")
        print(f"Retrieving {TEST_PROFILE_USERNAME}...")

        profile = await scraper.get_profile_data(
            TEST_PROFILE_USERNAME
        )

        print("\nProfile retrieved successfully.")
        print("=" * 50)
        print(f"Username: {profile.username}")
        print(f"Date joined: {profile.date_joined or 'Unknown'}")
        print(f"Nationality: {profile.nationality or 'Not set'}")
        print(f"Nation code: {profile.nation_code or 'Not set'}")
        print(f"Resistance avatar: {profile.avatar_res_url or 'Not set'}")
        print(f"Spy avatar: {profile.avatar_spy_url or 'Not set'}")

        print("\nGeneral statistics")
        print("-" * 50)
        print(f"Games played: {profile.total_games_played}")
        print(f"Wins: {profile.total_wins}")
        print(f"Losses: {profile.total_losses}")
        print(
            f"Overall win rate: "
            f"{profile.total_win_rate_percent:.1f}%"
        )

        print("\nResistance statistics")
        print("-" * 50)
        print(f"Games played: {profile.total_res_games}")
        print(f"Wins: {profile.total_res_wins}")
        print(f"Losses: {profile.total_res_losses}")
        print(
            f"Win rate: "
            f"{profile.resistance_win_rate_percent:.1f}%"
        )

        print("\nSpy statistics")
        print("-" * 50)
        print(f"Games played: {profile.total_spy_games}")
        print(f"Wins: {profile.total_spy_wins}")
        print(f"Losses: {profile.total_spy_losses}")
        print(
            f"Win rate: "
            f"{profile.spy_win_rate_percent:.1f}%"
        )

        print("\nRole statistics")
        print("-" * 50)

        if not profile.role_stats:
            print("No role statistics were returned.")
        else:
            for game_size, roles in sorted(
                profile.role_stats.items()
            ):
                print(f"\n{game_size} games:")

                for role_name, record in sorted(
                    roles.items()
                ):
                    display_role = role_name.replace(
                        "_",
                        " ",
                    ).title()

                    print(
                        f"  {display_role}: "
                        f"{record.games_played} games, "
                        f"{record.wins}W/{record.losses}L, "
                        f"{record.win_rate_percent:.1f}% WR"
                    )

        print("\nRole alliances")
        print("-" * 50)

        if not profile.role_alliances:
            print("No role-alliance data was returned.")
        else:
            for role_name, alignment in sorted(
                profile.role_alliances.items()
            ):
                print(f"{role_name}: {alignment}")

        print("=" * 50)

    except ProAvalonUserNotFound as error:
        print(f"User not found: {error}")

    except ProAvalonError as error:
        print(f"ProAvalon error: {error}")

    except Exception as error:
        print(
            f"Unexpected {type(error).__name__}: {error}"
        )
        raise

    finally:
        await scraper.close()


if __name__ == "__main__":
    asyncio.run(main())