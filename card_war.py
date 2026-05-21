import itertools
import random

# Define the ranks and suits
ranks = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
suits = ['Hearts', 'Diamonds', 'Clubs', 'Spades']

# Create the deck of cards
deck = list(itertools.product(ranks, suits))

# Shuffle the deck
random.shuffle(deck)

# Split the deck into two halves
player1_deck = deck[:26]
player2_deck = deck[26:]

# Function to compare two cards
def compare_cards(card1, card2):
    rank_order = {rank: i for i, rank in enumerate(ranks)}
    if rank_order[card1[0]] > rank_order[card2[0]]:
        return 1
    elif rank_order[card1[0]] < rank_order[card2[0]]:
        return -1
    else:
        return 0

# Initialize scores
player1_score = 0
player2_score = 0

# Run 10 rounds
for round in range(1, 11):
    # Draw a card from each player's deck
    player1_card = player1_deck.pop(0)
    player2_card = player2_deck.pop(0)
    
    # Compare the cards
    result = compare_cards(player1_card, player2_card)
    
    # Update scores
    if result == 1:
        player1_score += 1
        print(f'Round {round}: Player 1 wins with {player1_card} over {player2_card}')
    elif result == -1:
        player2_score += 1
        print(f'Round {round}: Player 2 wins with {player2_card} over {player1_card}')
    else:
        print(f'Round {round}: It's a tie with {player1_card} and {player2_card}')

# Print final scores
print(f'Final Scores: Player 1: {player1_score}, Player 2: {player2_score}')
if player1_score > player2_score:
    print('Player 1 wins the game!')
elif player1_score < player2_score:
    print('Player 2 wins the game!')
else:
    print('The game is a tie!')