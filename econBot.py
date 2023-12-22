import discord
from discord.ext import commands
import asyncio, aiosqlite
import datetime
from datetime import datetime
import pytz
import random
import math

bot = commands.Bot(command_prefix="$", intents=discord.Intents.all())
#TOKEN = '(insert token here)'

@bot.event
async def on_ready():
    bot.db = await aiosqlite.connect("bot.db")
    await asyncio.sleep(3)
    async with bot.db.cursor() as cursor:
        await cursor.execute("CREATE TABLE IF NOT EXISTS bank(wallet INTEGER, bank INTEGER, crypto INTEGER, miner INTEGER, user INTEGER)")
    await bot.db.commit()
    
    print('Database ready')
    print(f'Bot connected as {bot.user}')

async def createBalance(user):
    async with bot.db.cursor() as cursor:
        await cursor.execute('INSERT INTO bank VALUES (?, ?, ?, ?, ?)', (0, 1000, 0, 0, user.id,))
    await bot.db.commit()
    return

async def checkUser(user):
    async with bot.db.cursor() as cursor:
        await cursor.execute('SELECT * FROM bank WHERE user = ?', (user.id,))
        data = await cursor.fetchone()
    await bot.db.commit()

    if data == None:
        return False
    else:
        return True

async def getBalance(user):
    if await checkUser(user) == True:
        async with bot.db.cursor() as cursor:
            await cursor.execute('SELECT * FROM bank WHERE user = ?', (user.id,))
            data = await cursor.fetchone()
        await bot.db.commit()
    else:
        await createBalance(user)
        wallet, bank, crypto, miner = 0, 1000, 0, 0
        return wallet, bank, crypto, miner

    wallet, bank, crypto, miner = data[0], data[1], data[2], data[3]
    return wallet, bank, crypto, miner
    
async def updateWallet(user, amount:int):
    if await checkUser(user) == True:
       pass
    else:
        await createBalance(user)

    async with bot.db.cursor() as cursor:
        await cursor.execute('SELECT wallet FROM bank WHERE user = ?', (user.id,))
        userWallet = await cursor.fetchone()
        await cursor.execute('UPDATE bank SET wallet = ? WHERE user = ?', (userWallet[0] + amount, user.id,))
    await bot.db.commit()
    return
    
async def updateBank(user, amount:int):
    if await checkUser(user) == True:
        pass
    else:
        await createBalance(user)
        
    async with bot.db.cursor() as cursor:
        await cursor.execute('SELECT bank FROM bank WHERE user = ?', (user.id,))
        userBank = await cursor.fetchone()
        await cursor.execute('UPDATE bank SET bank = ? WHERE user = ?', (userBank[0] + amount, user.id,))
    await bot.db.commit()
    return

async def updateCrypto(user, amount:int):
    if await checkUser(user) == True:
        async with bot.db.cursor() as cursor:
            await cursor.execute('SELECT crypto FROM bank WHERE user = ?', (user.id,))
            userCrypto = await cursor.fetchone()
            await cursor.execute('UPDATE bank SET crypto = ? WHERE user = ?', (userCrypto[0] + amount, user.id,))
        await bot.db.commit()
        return
    else:
        await createBalance(user)
        async with bot.db.cursor() as cursor:
            await cursor.execute('UPDATE bank SET crypto = ? WHERE user = ?', (amount, user.id,))
        await bot.db.commit()
        return
    
async def upgradeMiner(user):
    if await checkUser(user) == True:
        pass
    else:
        await createBalance(user)

    async with bot.db.cursor() as cursor:
        await cursor.execute('SELECT miner FROM bank WHERE user = ?', (user.id,))
        currentLevel = await cursor.fetchone()
        await cursor.execute('UPDATE bank SET miner = ? WHERE user = ?', (currentLevel[0] + 1, user.id,))
    await bot.db.commit()
    return

async def adminMiner(user):
    async with bot.db.cursor() as cursor:
        await cursor.execute('UPDATE bank SET miner = ? WHERE user = ?', (999, user.id,))
    await bot.db.commit()
    return

async def getLeaders():
    async with bot.db.cursor() as cursor:
        await cursor.execute('SELECT wallet, bank, crypto, user FROM bank WHERE wallet+bank+(crypto*10000) > 0 ORDER BY wallet+bank+(crypto*10000) DESC')
        data = await cursor.fetchmany(10)
    await bot.db.commit()
    
    leadersFMT = []

    for entry in data:
        entryList = list(entry)
        entryList[0] = entry[0] + entry[1] + (entry[2]*10000)
        entryList[1] = entry[3]
        del entryList[2:4]
        entry = tuple(entryList)
        leadersFMT.append(entry)

    return sorted(leadersFMT, reverse=True)

async def getCrypto():
    async with bot.db.cursor() as cursor:
        await cursor.execute('SELECT crypto FROM bank WHERE crypto > 0')
        data = await cursor.fetchall()
    await bot.db.commit()

    cryptoList = []

    for entry in data:
        cryptoList.append(entry[0])
    
    return sum(cryptoList)

async def getWallets():
    async with bot.db.cursor() as cursor:
        await cursor.execute('SELECT wallet, user FROM bank WHERE wallet > 0 ORDER BY wallet DESC')
        wallets = await cursor.fetchmany(10)
    await bot.db.commit()

    return wallets

async def resetUser(user):
    async with bot.db.cursor() as cursor:
        await cursor.execute('DELETE from BANK where user = ?', (user.id,))
    await bot.db.commit()
    return

@bot.command()
async def balance(ctx, user:discord.Member=None):
    """Used to check your balance"""
    timeStamp = datetime.now(pytz.timezone('America/Chicago'))
    user = user or ctx.author
    wallet, bank, crypto, miner = await getBalance(user)

    embed = discord.Embed(title=f"{user.display_name}'s Balance", color=0x13ff00, timestamp=timeStamp)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name='Wallet:', value=f"${wallet:,}", inline=True)
    embed.add_field(name='Bank:', value=f"${bank:,}", inline=True)
    embed.add_field(name='Flyxcoin:', value=f"{crypto:,}", inline=False)
    embed.add_field(name='Miner Level:', value=f"Level {miner}", inline=True)
    embed.add_field(name='Total Net Worth', value=f"${wallet + bank + (crypto*10000):,}", inline=False)

    await ctx.send(embed=embed)

@bot.command()
@commands.is_owner()
async def adminme(ctx):
    await adminMiner(ctx.author)
    return

@bot.command()
async def deposit(ctx, amount:int=None):
    """Deposits money from your wallet into your bank account"""
    wallet, bank, crypto, miner = await getBalance(ctx.author)
    amount = amount or wallet

    if amount <= wallet:
        await updateWallet(ctx.author, -amount)
        await updateBank(ctx.author, amount)
        await ctx.send(f"Successfully deposited ${amount:,}")
        return
    else:
        await ctx.send(f"{ctx.author.mention} You cannot deposit more than your current wallet balance.")
        return
    
@bot.command()
async def withdraw(ctx, amount:int=None):
    """Withdraws money from your bank account into your wallet"""
    wallet, bank, crypto, miner = await getBalance(ctx.author)
    amount = amount or bank

    if amount <= bank:
        await updateBank(ctx.author, -amount)
        await updateWallet(ctx.author, amount)
        await ctx.send(f"Successfully withdrawn ${amount:,}")
        return
    else:
        await ctx.send(f"{ctx.author.mention} You cannot withdraw more than your current bank balance.")
        return
    
@bot.command()
@commands.cooldown(1, 3, commands.BucketType.user)
async def beg(ctx):
    """Beg the economy gods for a small amount of money"""
    amount = random.randint(1, 100)

    if random.randint(1, 2) == 1:
        await ctx.send('You got nothing.')
        return
    else:
        await updateWallet(ctx.author, amount)
        await ctx.send(f"You got ${amount}")
        return
@beg.error
async def begError(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"{ctx.author.mention} That command is on cooldown. It can be used once every 3 seconds.")
        return
    else:
        await ctx.send(error)
        return
    
@bot.command()
@commands.cooldown(1, 3600, commands.BucketType.user)
async def mine(ctx):
    """Uses your Flyxcoin miner to mine Flyxcoin"""
    wallet, bank, crypto, miner = await getBalance(ctx.author)

    if miner == 0:
        await ctx.send(f"{ctx.author.mention} You need to buy a Flyxcoin miner to mine!")
        return
    elif ctx.author.id == 989732316123389957:
        await updateCrypto(ctx.author, 1)
        await ctx.send('You mined a Flyxcoin!')
        return
    elif miner == 1:
        if random.randint(1, 100) == 1:
            await updateCrypto(ctx.author, 1)
            await ctx.send('You mined a Flyxcoin!')
            return
        else:
            await ctx.send('You mined nothing.')
            return
    elif miner == 2:
        if random.randint(1, 100) in range(1, 5):
            await updateCrypto(ctx.author, 1)
            await ctx.send('You mined a Flyxcoin!')
            return
        else:
            await ctx.send('You mined nothing.')
            return
    elif miner == 3:
        if random.randint(1, 100) in range(1, 10):
            await updateCrypto(ctx.author, 1)
            await ctx.send('You mined a Flyxcoin!')
            return
        else:
            await ctx.send('You mined nothing.')
            return
    elif miner == 4:
        if random.randint(1, 100) in range(1, 15):
            await updateCrypto(ctx.author, 1)
            await ctx.send('You mined a Flyxcoin!')
            return
        else:
            await ctx.send('You mined nothing.')
            return
    elif miner == 5:
        if random.randint(1, 100) in range(1, 20):
            await updateCrypto(ctx.author, 1)
            await ctx.send('You mined a Flyxcoin!')
            return
        else:
            await ctx.send('You mined nothing.')
            return
    elif miner == 999:
        await updateCrypto(ctx.author, 10)
        await ctx.send('You mined 10 Flyxcoin!')
        return
@mine.error
async def mineError(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"{ctx.author.mention} That command is on cooldown. It can be used once every hour.")
        return
    else:
        await ctx.send(error)
        return

@bot.command()
async def flx(ctx, action:str=None, amount:int=None, user:discord.Member=None):
    """Used for buying, selling, and sending Flyxcoin"""
    timeStamp = datetime.now(pytz.timezone('America/Chicago'))
    if not action:
        cryptoTotal = await getCrypto()

        embed = discord.Embed(title='Total Flyxcoin in circulation.', color=0x13ff00, timestamp=timeStamp)
        embed.add_field(name='Total FLX in circulation:', value=f"{cryptoTotal:,}", inline=False)
        embed.add_field(name='Total value of all circulating FLX:', value=f"${cryptoTotal*10000:,}")
        await ctx.send(embed=embed)
        return
    
    wallet, bank, crypto, miner = await getBalance(ctx.author)
   
    if action.lower() == 'buy':
        amount = amount or math.trunc(bank/10000)
        cost = amount*10000
        if cost <= bank:
            await updateBank(ctx.author, -cost)
            await updateCrypto(ctx.author, amount)
            await ctx.send(f"{ctx.author.mention} You purchased {amount} Flyxcoin!")
            return
        else:
            await ctx.send(f"{ctx.author.mention} You have insufficient funds.")
            return
    elif action.lower() == 'sell':
        amount = amount or crypto
        cost = amount*10000
        if crypto >= amount:
            await updateCrypto(ctx.author, -amount)
            await updateBank(ctx.author, cost)
            await ctx.send(f"{ctx.author.mention} You sold {amount:,} Flyxcoin for ${cost:,}")
            return
        else:
            await ctx.send(f"{ctx.author.mention} You have insufficient Flyxcoin.")
            return
    elif action.lower() == 'send':
        if not user:
            await ctx.send(f"{ctx.author.mention} You need to specify a user to send Flyxcoin to.")
            return
        else:
            if crypto >= amount:
                await updateCrypto(ctx.author, -amount)
                await updateCrypto(user, amount)
                await ctx.send(f"{ctx.author.mention} You sent {amount} Flyxcoin to {user.mention}")
                return
            else:
                await ctx.send(f"{ctx.author.mention} You have insufficient Flyxcoin.")
                return

@bot.command()
async def leaderboard(ctx):
    timeStamp = datetime.now(pytz.timezone('America/Chicago'))
    """Shows the richest members"""
    leaders = await getLeaders()
    leaderList = []

    for leader in leaders:
        leaderList.append(f"{leaders.index(leader)+1}. ${leader[0]:,} - <@{leader[1]}>")

    response = '\n'.join(leaderList)

    embed = discord.Embed(title='Top 10 Richest Users', description=f"Based on total net worth \n\n {response}", color=0x13ff00, timestamp=timeStamp)
    await ctx.send(embed=embed)
    return

@bot.command()
async def wallets(ctx):
    """Get a list of the biggest undeposited wallets"""
    timeStamp = datetime.now(pytz.timezone('America/Chicago'))
    wallets = await getWallets()
    
    respList = []

    for wallet in wallets:
        respList.append(f"{wallets.index(wallet)+1}. ${wallet[0]:,} - <@{wallet[1]}>")

    response = '\n'.join(respList)

    embed = discord.Embed(title='Top undeposited wallets', description=response , color=0x13ff00, timestamp=timeStamp)

    await ctx.send(embed=embed)

@bot.command()
@commands.cooldown(1, 3600, commands.BucketType.user)
async def rob(ctx, user:discord.Member):
    """Attempt to rob someone for their wallet balance"""
    if not user:
        await ctx.send(f"{ctx.author.mention} You need to specify a user to rob.")
        return
    elif user == ctx.author:
        await ctx.send(f"{ctx.author.mention} You cannot rob yourself.")
    else:
        pass

    wallet, bank, crypto, miner = await getBalance(user)

    if wallet == 0:
        await ctx.send(f"{ctx.author.mention} You can't rob someone with no money in their wallet.")
        return
    else:
        if random.randint(1, 2) == 1:
            await ctx.send('Robbery attempt failed. Try again in an hour.')
            return
        else:
            amount = random.randint(1, wallet)

            await updateWallet(user, -amount)
            await updateWallet(ctx.author, amount)
            await ctx.send(f"You robbed ${amount:,} from {user.mention}")
            return
@rob.error
async def robError(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"{ctx.author.mention} That command is on cooldown, it can be used once every hour.")
        return
    else:
        await ctx.send(error)
        return

@bot.command()
async def upgrade(ctx):
    """Upgrades your Flyxcoin miner"""
    wallet, bank, crypto, miner = await getBalance(ctx.author)

    if miner == 0 and bank >= 100:
        await updateBank(ctx.author, -100)
        await upgradeMiner(ctx.author)
        await ctx.send('Miner upgraded to level 1! You have a 1% chance of mining a Flyxcoin!')
        return
    elif miner == 1 and bank >= 5000:
        await updateBank(ctx.author, -5000)
        await upgradeMiner(ctx.author)
        await ctx.send('Miner upgraded to level 2! You have a 5% chance of mining a Flyxcoin!')
        return
    elif miner == 2 and bank >= 20000:
        await updateBank(ctx.author, -20000)
        await upgradeMiner(ctx.author)
        await ctx.send('Miner upgraded to level 3! You have a 10% chance of mining a Flyxcoin!')
        return
    elif miner == 3 and bank >= 100000:
        await updateBank(ctx.author, -100000)
        await upgradeMiner(ctx.author)
        await ctx.send('Miner upgraded to level 4! You have a 15% chance of mining a Flyxcoin!')
        return
    elif miner == 4 and bank >= 500000:
        await updateBank(ctx.author, -500000)
        await upgradeMiner(ctx.author)
        await ctx.send('Miner upgraded to level 5! You have a 20% chance of mining a Flyxcoin!')
        return
    else:
        await ctx.send('Either your miner is already max level, or you cannot afford the next upgrade(draws from your bank).')
        return

@bot.command()
async def coinflip(ctx, guess:str=None, bet:int=None):
    """Bet on a coinflip (2x bet payout)"""
    if not guess:
        await ctx.send(f"{ctx.author.mention} You need to specify either `heads` or `tails`.")
        return
    else:
        pass

    if not bet:
        await ctx.send(f"{ctx.author.mention} You need to specify an amount to bet.")
        return
    else:
        pass

    wallet, bank, crypto, miner = await getBalance(ctx.author)

    coin = ['tails', 'heads']

    if bet > wallet:
        await ctx.send(f"{ctx.author.mention} You can only bet as much as you have in your wallet.")
        return
    else:
        await updateWallet(ctx.author, -bet)
        flip = random.choice(coin)

        if guess.lower() == flip:
            await ctx.send(f"It's **{flip}**. You win **${bet*2:,}**")
            await updateWallet(ctx.author, bet*2)
            return
        elif guess.lower() != 'heads' and guess.lower() != 'tails':
            await ctx.send('Invalid guess. Try `heads` or `tails`.')
            await updateWallet(ctx.author, bet)
            return
        else:
            await ctx.send(f"It's **{flip}**, you lose **${bet:,}**")
            return
        
@bot.command()
async def rps(ctx, guess:str=None, bet:int=None):
    """Bet on a game of Rock Paper Scissors (2x bet payout)"""
    wallet, bank, crypto, miner = await getBalance(ctx.author)

    if bet > wallet:
        await ctx.send(f"{ctx.author.mention} You can only bet as much as you have in your wallet.")
        return
    else:
        pass

    if not guess:
        await ctx.send(f"{ctx.author.mention} You must play either `rock`, `paper`, or `scissors`.")
        return
    else:
        pass

    if not bet:
        await ctx.send(f"{ctx.author.mention} You must specify an amount to bet.")
        return
    else:
        pass

    moves = ['rock', 'paper', 'scissors']

    if guess.lower() not in moves:
        await ctx.send(f"{ctx.author.mention} Invalid guess. You must play either `rock`, `paper`, or `scissors`.")
        return
    else:
        pass

    botMove = random.choice(moves)
    await updateWallet(ctx.author, -bet)

    if guess.lower() == botMove:
        await ctx.send(f"The bot chose **{botMove}**. It's a tie!")
        await updateWallet(ctx.author, bet)

    elif guess.lower() == 'rock':
        if botMove == 'paper':
            await ctx.send(f"The bot chose **{botMove}**. You lose **${bet:,}**")
            return
        else:
            await ctx.send(f"The bot chose **{botMove}**. You win **${bet*2:,}!**")
            await updateWallet(ctx.author, bet*3)
            return
    elif guess.lower() == 'paper':
        if botMove == 'scissors':
            await ctx.send(f"The bot chose **{botMove}**. You lose **${bet:,}**")
            return
        else:
            await ctx.send(f"The bot chose **{botMove}**. You win **${bet*2:,}!**")
            await updateWallet(ctx.author, bet*3)
            return
    elif guess.lower() == 'scissors':
        if botMove == 'rock':
            await ctx.send(f"The bot chose **{botMove}**. You lose **${bet:,}**")
            return
        else:
            await ctx.send(f"The bot chose **{botMove}**. You win **${bet*2:,}!**")
            await updateWallet(ctx.author, bet*3)
            return

@bot.command()
async def dice(ctx, guess:int=None, bet:int=None):
    """Bet on a standard 6-sided dice roll(6x bet payout)"""
    wallet, bank, crypto, miner = await getBalance(ctx.author)

    if not guess:
        await ctx.send(f"{ctx.author.mention} You need to guess a number 1-6.")
        return
    else:
        pass

    if not bet:
        await ctx.send(f"{ctx.author.mention} You need to specify a bet amount.")
        return
    else:
        pass

        roll = random.randint(1, 6)
        
        if bet <= wallet:
            await updateWallet(ctx.author, -bet)
        else:
            await ctx.send('You can only bet as much as you have in your wallet.')
            return

        if guess == roll:
            await ctx.send(f"You rolled a **{roll}**. You win **${bet*6:,}**")
            await updateWallet(ctx.author, bet*6)
            return
        else:
            await ctx.send(f"You rolled a **{roll}**. You lose **${bet:,}**")
            return

@bot.command()
async def roulette(ctx, bet:str=None, betAmount:int=None):
    """Bet on a traditional game of roulette(up to 35x bet payout)\n Red: 1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36\n Black: 2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35"""

    if not bet:
        await ctx.send(f"{ctx.author.mention} You need to place a bet on either `red` `black` or a number `00` or `0-36`.")
        return
    else:
        pass

    if not betAmount:
        await ctx.send(f"{ctx.author.mention} You need to place a bet.")
        return
    else:
        pass

    wallet, bank, crypto, miner = await getBalance(ctx.author)

    if betAmount > wallet:
        await ctx.send(f"{ctx.author.mention} You cannot bet more than you have in your wallet.")
        return
    else:
        await updateWallet(ctx.author, -betAmount)

    # The roulette wheel has 38 slots, numbered 0 through 36, and a 00 slot
    slots = [0, 00, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36]

    if bet.lower() != 'red' and bet.lower() != 'black':
        bet = int(bet)
    else:
        pass

    # The roulette wheel is spun and the ball lands on a random slot
    outcome = random.choice(slots)
    await ctx.send(f"And the roll is..... **{outcome}**")
    # If the player bet on a number and the ball lands on that number, they win 35 times their bet
    if bet == outcome:
        await ctx.send(f"Congratulations {ctx.author.mention}, you won **${betAmount*35:,}**")
        await updateWallet(ctx.author, betAmount*35)
        return
    # If the player bet on a color and the ball landed on a slot of that color, they win 2 times their bet
    elif bet == "red" and (outcome in [1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]):
        await ctx.send(f"Congratulations {ctx.author.mention}, you won **${betAmount*2:,}**")
        await updateWallet(ctx.author, betAmount*2)
        return
    elif bet == "black" and (outcome in [2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35]):
        await ctx.send(f"Congratulations {ctx.author.mention}, you won **${betAmount*2:,}**")
        await updateWallet(ctx.author, betAmount*2)
        return
    # If the player's bet did not win, they lose their bet
    else:
        await ctx.send("Sorry, you lost your bet.")
        return

@bot.command()
@commands.cooldown(1, 86400, commands.BucketType.user)
async def daily(ctx):
    """A daily payout of 10% of your bank balance"""
    wallet, bank, crypto, miner = await getBalance(ctx.author)
    payout = math.trunc(bank*.1)
    await updateBank(ctx.author, payout)
    await ctx.send(f'You recieved your daily payout of **${payout:,}**')
    return
@daily.error
async def dailyError(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"{ctx.author.mention} That command is on cooldown. It can be used once every 24 hours.")
        return
    else:
        await ctx.send(error)
        return

@bot.command()
@commands.is_owner()
async def adminmine(ctx, amount:int):
    await updateCrypto(ctx.author, amount) 
    return

@bot.command()
@commands.is_owner()
async def reset(ctx, user:discord.Member):
    if not user:
        await ctx.send('You need to specify a user to reset.')
        return
    else:
        await resetUser(user)
        await ctx.send('User reset.')
        return

bot.run(TOKEN)