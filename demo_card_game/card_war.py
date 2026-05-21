import itertools
import random

# 创建牌组
suits = ['♠', '♥', '♦', '♣']
ranks = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']

# 生成所有可能的牌
deck = list(itertools.product(suits, ranks))

# 洗牌
random.shuffle(deck)

# 分配牌
player1_deck = deck[:26]
player2_deck = deck[26:]

# 比较牌
def compare_cards(card1, card2):
    rank_order = {rank: idx for idx, rank in enumerate(ranks)}
    if rank_order[card1[1]] > rank_order[card2[1]]:
        return 1
    elif rank_order[card1[1]] < rank_order[card2[1]]:
        return 2
    else:
        return 0

# 累计分数
player1_score = 0
player2_score = 0

# 运行 10 回合
for round in range(1, 11):
    card1 = player1_deck.pop(0)
    card2 = player2_deck.pop(0)
    print(f'Round {round}: Player 1: {card1[1]}{card1[0]}, Player 2: {card2[1]}{card2[0]}')
    result = compare_cards(card1, card2)
    if result == 1:
        player1_score += 1
        print('Player 1 wins this round!')
    elif result == 2:
        player2_score += 1
        print('Player 2 wins this round!')
    else:
        print('It\'s a tie!')

# 输出结果
print(f'Final Score: Player 1: {player1_score}, Player 2: {player2_score}')