import discord
from discord import app_commands
from discord.ui import View, Button, Select
import random, json, os, asyncio, time
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_IDS = os.getenv("GUILD_IDS", "").split(",") if os.getenv("GUILD_IDS") else []

# (La gestion par guild a été retirée) Les commandes sont synchronisées globalement.

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# Centralized persistence in data.json
DATA_FILE = "data.json"
if os.path.exists(DATA_FILE) and os.path.getsize(DATA_FILE) > 0:
    with open(DATA_FILE, "r") as f:
        data = json.load(f)
else:
    data = {
        "bank": {},
        "daily": {},
        "voc": {},
        "settings": {"voc_role_rules": []}
    }

# Ensure keys exist
data.setdefault("bank", {})
data.setdefault("daily", {})
data.setdefault("voc", {})
data.setdefault("settings", {"voc_role_rules": []})

# Local references (these are views into `data`)
bank = data["bank"]
daily_data = data["daily"]
voc_data = data["voc"]
settings = data["settings"]

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

def save():
    save_data()

def save_daily():
    save_data()

def save_voc():
    save_data()

def save_settings():
    save_data()

@bot.event
async def on_voice_state_update(member, before, after):
    user_id = str(member.id)
    now = time.time()
    # Entrée en vocal
    if before.channel is None and after.channel is not None:
        voc_data.setdefault(user_id, {"total": 0, "last_join": None})
        voc_data[user_id]["last_join"] = now
        save_voc()
    # Sortie de vocal
    elif before.channel is not None and after.channel is None:
        if user_id in voc_data and voc_data[user_id].get("last_join"):
            session = now - voc_data[user_id]["last_join"]
            voc_data[user_id]["total"] += int(session)
            voc_data[user_id]["last_join"] = None
            save_voc()


async def _voc_updater_loop():
    # periodically flush live session time into total every minute
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = time.time()
        changed = False
        for uid, data in list(voc_data.items()):
            lj = data.get("last_join")
            if lj:
                delta = now - lj
                if delta >= 1:
                    voc_data[uid]["total"] += int(delta)
                    voc_data[uid]["last_join"] = now
                    changed = True
        if changed:
            save_voc()
        # Enforce voc role rules periodically (also runs every loop)
        # settings["voc_role_rules"]: list of {min_seconds, max_seconds, role_id}
        now = time.time()
        # Build a mapping of uid -> total seconds including ongoing session
        totals = {}
        for uid, data in voc_data.items():
            t = data.get("total", 0)
            if data.get("last_join"):
                t += int(now - data.get("last_join"))
            totals[uid] = t

        # For each guild, try to assign/remove roles for members present
        for guild in bot.guilds:
            for member in guild.members:
                uid = str(member.id)
                if uid not in totals:
                    user_total = 0
                else:
                    user_total = totals[uid]

                # Determine which rules match
                matched_role_ids = [r["role_id"] for r in settings.get("voc_role_rules", []) if r["min_seconds"] <= user_total <= r["max_seconds"]]

                # Assign roles that match and the member doesn't have
                for rid in matched_role_ids:
                    try:
                        role = guild.get_role(int(rid))
                        if role and role not in member.roles:
                            await member.add_roles(role, reason="Rôle voc automatique")
                    except Exception:
                        pass

                # Optionally remove roles that were assigned by rules but no longer match
                # We only remove roles that are present in the rules to avoid touching other roles
                rule_role_ids = [r["role_id"] for r in settings.get("voc_role_rules", [])]
                for rid in rule_role_ids:
                    try:
                        role = guild.get_role(int(rid))
                        if role and role in member.roles and rid not in matched_role_ids:
                            await member.remove_roles(role, reason="Rôle voc automatique - condition non remplie")
                    except Exception:
                        pass
        await asyncio.sleep(60)

# ---------------- Gestion des écus ----------------
def get_balance(user_id):
    if str(user_id) not in bank:
        bank[str(user_id)] = 1000
        save()
    return bank[str(user_id)]

def update_balance(user_id, amount):
    bank[str(user_id)] = get_balance(user_id) + amount
    save()

def can_claim_daily(user_id):
    """Vérifie si l'utilisateur peut récupérer son daily"""
    user_str = str(user_id)
    if user_str not in daily_data:
        return True
    
    last_claim = daily_data[user_str]
    current_time = time.time()
    # 86400 secondes = 24 heures
    return (current_time - last_claim) >= 86400

def claim_daily(user_id):
    """Enregistre que l'utilisateur a récupéré son daily"""
    daily_data[str(user_id)] = time.time()
    save_daily()

def time_until_next_daily(user_id):
    """Retourne le temps restant en secondes avant le prochain daily"""
    user_str = str(user_id)
    if user_str not in daily_data:
        return 0
    
    last_claim = daily_data[user_str]
    current_time = time.time()
    time_passed = current_time - last_claim
    time_remaining = 86400 - time_passed
    return max(0, time_remaining)

# ---------------- Logging ----------------
def log(message):
    print(f"[LOG] {message}")

# ---------------- BLACKJACK ----------------
class BlackjackView(View):
    def __init__(self, player_hand, dealer_hand, mise, user_id):
        super().__init__(timeout=120)
        self.player_hand = player_hand
        self.dealer_hand = dealer_hand
        self.mise = mise
        self.user_id = user_id
        self.finished = False

    def score(self, hand):
        s = sum(hand)
        while s > 21 and 11 in hand:
            hand[hand.index(11)] = 1
            s = sum(hand)
        return s

    async def end_game(self, interaction, result_msg, color):
        embed = discord.Embed(title="🃏 Blackjack", color=color)
        embed.add_field(name="Ta main", value=f"{self.player_hand} → {self.score(self.player_hand)}", inline=False)
        embed.add_field(name="Main du croupier", value=f"{self.dealer_hand} → {self.score(self.dealer_hand)}", inline=False)
        embed.add_field(name="Résultat", value=result_msg, inline=False)
        embed.add_field(name="Nouveau solde", value=f"{get_balance(self.user_id)} écus", inline=False)
        self.finished = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)
        log(f"[BLACKJACK] {interaction.user} → {result_msg} (Solde: {get_balance(self.user_id)})")

    @discord.ui.button(label="Hit 🟢", style=discord.ButtonStyle.green)
    async def hit(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.user_id or self.finished:
            return
        self.player_hand.append(random.choice([2,3,4,5,6,7,8,9,10,10,10,10,11]))
        player_score = self.score(self.player_hand)
        log(f"[BLACKJACK] {interaction.user} tire une carte: {self.player_hand[-1]} → main: {self.player_hand}")
        if player_score > 21:
            update_balance(self.user_id, -self.mise)
            await self.end_game(interaction, f"💥 Tu dépasses 21, tu perds {self.mise} écus", discord.Color.red())
        else:
            embed = discord.Embed(title="🃏 Blackjack", color=discord.Color.blurple())
            embed.add_field(name="Ta main", value=f"{self.player_hand} → {player_score}", inline=False)
            embed.add_field(name="Main du croupier", value=f"{self.dealer_hand[0]} + ❓", inline=False)
            embed.add_field(name="Action", value="Choisis Hit 🟢 ou Stand 🔴", inline=False)
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Stand 🔴", style=discord.ButtonStyle.red)
    async def stand(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.user_id or self.finished:
            return
        while self.score(self.dealer_hand) < 17:
            self.dealer_hand.append(random.choice([2,3,4,5,6,7,8,9,10,10,10,10,11]))
        player_score = self.score(self.player_hand)
        dealer_score = self.score(self.dealer_hand)
        if dealer_score > 21 or player_score > dealer_score:
            update_balance(self.user_id, self.mise)
            await self.end_game(interaction, f"🎉 Tu gagnes {self.mise} écus", discord.Color.green())
        elif dealer_score == player_score:
            await self.end_game(interaction, "🤝 Égalité, ta mise est rendue.", discord.Color.blurple())
        else:
            update_balance(self.user_id, -self.mise)
            await self.end_game(interaction, f"😢 Tu perds {self.mise} écus", discord.Color.red())


@tree.command(name="blackjack", description="Jouer au blackjack interactif")
async def blackjack(interaction: discord.Interaction, mise: int):
    balance = get_balance(interaction.user.id)
    if mise <= 0 or mise > balance:
        await interaction.response.send_message("❌ Mise invalide.", ephemeral=True)
        return
    player_hand = [random.choice([2,3,4,5,6,7,8,9,10,10,10,10,11]) for _ in range(2)]
    dealer_hand = [random.choice([2,3,4,5,6,7,8,9,10,10,10,10,11]) for _ in range(2)]
    embed = discord.Embed(title="🃏 Blackjack", color=discord.Color.blurple())
    embed.add_field(name="Ta main", value=f"{player_hand} → {sum(player_hand)}", inline=False)
    embed.add_field(name="Main du croupier", value=f"{dealer_hand[0]} + ❓", inline=False)
    embed.add_field(name="Action", value="Choisis Hit 🟢 ou Stand 🔴", inline=False)
    view = BlackjackView(player_hand, dealer_hand, mise, interaction.user.id)
    log(f"[BLACKJACK] {interaction.user} démarre une partie avec mise: {mise}")
    await interaction.response.send_message(embed=embed, view=view)


# ---------------- ROULETTE ----------------
class RouletteView(View):
    def __init__(self, user_id):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.mises = {}
        self.selected_case = None
        self.finished = False
        self.message = None

        # Menu case
        options_case = [
            discord.SelectOption(label="Rouge", value="color:rouge"),
            discord.SelectOption(label="Noir", value="color:noir"),
            discord.SelectOption(label="Pair", value="parity:pair"),
            discord.SelectOption(label="Impair", value="parity:impair"),
            discord.SelectOption(label="1-12", value="dozen:1-12"),
            discord.SelectOption(label="13-24", value="dozen:13-24"),
            discord.SelectOption(label="25-36", value="dozen:25-36"),
        ]
        self.case_select = Select(placeholder="Choisis une case", options=options_case)
        self.case_select.callback = self.case_callback
        self.add_item(self.case_select)

        # Menu mise
        options_mise = [
            discord.SelectOption(label="10", value="10"),
            discord.SelectOption(label="50", value="50"),
            discord.SelectOption(label="100", value="100"),
            discord.SelectOption(label="200", value="200"),
        ]
        self.mise_select = Select(placeholder="Choisis une mise", options=options_mise)
        self.mise_select.callback = self.mise_callback
        self.add_item(self.mise_select)

        self.launch_button = Button(label="Lancer la roulette", style=discord.ButtonStyle.red)
        self.launch_button.callback = self.launch_callback
        self.add_item(self.launch_button)

    async def case_callback(self, interaction):
        if interaction.user.id != self.user_id:
            return
        self.selected_case = self.case_select.values[0]
        await interaction.response.send_message(f"✅ Tu as choisi **{self.selected_case}**, maintenant choisis ta mise.", ephemeral=True)

    async def mise_callback(self, interaction):
        if interaction.user.id != self.user_id:
            return
        if not self.selected_case:
            await interaction.response.send_message("❌ Choisis d'abord une case.", ephemeral=True)
            return
        mise = int(self.mise_select.values[0])
        balance_before = get_balance(self.user_id)
        if mise > balance_before:
            await interaction.response.send_message("❌ Solde insuffisant.", ephemeral=True)
            return
        if self.selected_case in self.mises:
            self.mises[self.selected_case] += mise
        else:
            self.mises[self.selected_case] = mise
        update_balance(self.user_id, -mise)
        balance_after = get_balance(self.user_id)

        log(f"[ROULETTE] {interaction.user} mise {mise} écus sur {self.selected_case}. Solde avant: {balance_before}, après: {balance_after}")

        # Mise à jour embed
        description = "Choisis une case et une mise via les menus.\n\n**Mises actuelles :**\n"
        for case, montant in self.mises.items():
            description += f"- {case} : {montant} écus\n"
        description += f"\n**Solde restant : {balance_after} écus**"
        embed = discord.Embed(title="🎡 Roulette", description=description, color=discord.Color.blurple())

        if self.message:
            await self.message.edit(embed=embed, view=self)
        else:
            await interaction.response.send_message(embed=embed, view=self)
            self.message = await interaction.original_response()
        self.selected_case = None

    async def launch_callback(self, interaction):
        if interaction.user.id != self.user_id or self.finished:
            return
        if not self.mises:
            await interaction.response.send_message("❌ Tu n'as misé sur aucune case !", ephemeral=True)
            return
        self.finished = True

        result_number = random.randint(0,36)
        color_map = {0:"vert"}
        rouge_numbers = [1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36]
        for i in range(1,37):
            color_map[i] = "rouge" if i in rouge_numbers else "noir"
        result_color = color_map[result_number]
        result_parity = "pair" if result_number!=0 and result_number%2==0 else ("impair" if result_number!=0 else "none")
        result_dozen = "1-12" if 1<=result_number<=12 else ("13-24" if 13<=result_number<=24 else ("25-36" if 25<=result_number<=36 else "none"))

        msg_result = ""
        for selection, mise in self.mises.items():
            typ,val = selection.split(":")
            gain = 0
            if typ=="number" and int(val)==result_number:
                gain = mise*35
            elif typ=="color" and val==result_color:
                gain = mise*2
            elif typ=="parity" and val==result_parity:
                gain = mise*2
            elif typ=="dozen" and val==result_dozen:
                gain = mise*3
            if gain>0:
                update_balance(self.user_id, gain)
                msg_result += f"✅ {selection} : +{gain} écus\n"
            else:
                msg_result += f"❌ {selection} : perdu {mise} écus\n"

        embed = discord.Embed(title="🎡 Roulette", description=f"La bille tombe sur **{result_number}** ({result_color})", color=discord.Color.gold())
        embed.add_field(name="Résultat des mises", value=msg_result, inline=False)
        embed.add_field(name="Solde actuel", value=f"{get_balance(self.user_id)} écus")
        await interaction.response.edit_message(embed=embed, view=None)
        log(f"[ROULETTE] {interaction.user} résultat: {result_number} ({result_color}) → Mises: {self.mises}")

@tree.command(name="roulette", description="Jouer à la roulette")
async def roulette(interaction: discord.Interaction):
    view = RouletteView(interaction.user.id)
    embed = discord.Embed(title="🎡 Roulette", description="Choisis une case et une mise via les menus.", color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, view=view)
    log(f"[ROULETTE] {interaction.user} démarre une partie")

# ---------------- MACHINE À SOUS ----------------
@tree.command(name="slots", description="Jouer à la machine à sous")
async def slots(interaction: discord.Interaction, mise:int):
    balance = get_balance(interaction.user.id)
    if mise<=0 or mise>balance:
        await interaction.response.send_message("❌ Mise invalide.", ephemeral=True)
        return
    update_balance(interaction.user.id, -mise)
    emojis = ["🍒","🍋","🍉","⭐","💎"]
    await interaction.response.send_message("🎰 Lancement...", ephemeral=False)
    msg = await interaction.original_response()

    # Create final 3x3 grid and animate briefly
    grid = [[random.choice(emojis) for _ in range(3)] for _ in range(3)]
    # simple animation: show final grid (could be enhanced)
    display = "\n".join([" | ".join(row) for row in grid])
    embed = discord.Embed(title="🎰 Machine à sous", description=display, color=discord.Color.blurple())
    await msg.edit(embed=embed)

    # Check win: first row all identical symbols
    first_row = grid[0]
    if all(x == first_row[0] for x in first_row):
        gain = mise * 5
        msg_result = f"🎉 JACKPOT ! Tu gagnes {gain} écus"
        color = discord.Color.green()
        update_balance(interaction.user.id, gain)
    else:
        gain = -mise
        msg_result = f"😢 Tu perds ta mise de {mise} écus"
        color = discord.Color.red()

    embed = discord.Embed(title="🎰 Machine à sous", description=display, color=color)
    embed.add_field(name="Résultat", value=msg_result)
    embed.add_field(name="Nouveau solde", value=f"{get_balance(interaction.user.id)} écus")
    await msg.edit(embed=embed)


# ---------------- LEADERBOARD ----------------
@tree.command(name="leaderboard", description="Affiche le top 10 des joueurs")
async def leaderboard(interaction: discord.Interaction):
    sorted_bank = sorted(bank.items(), key=lambda x: x[1], reverse=True)
    description = ""
    for i, (user_id, credits) in enumerate(sorted_bank[:10], start=1):
        user = await bot.fetch_user(int(user_id))
        description += f"{i}. {user.name} → {credits} écus\n"
    embed = discord.Embed(title="🏆 Classement général", description=description, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)
    log(f"[LEADERBOARD] {interaction.user} a affiché le classement")


# ---------------- DAILY CREDITS ----------------
@tree.command(name="daily", description="Récupérer tes écus quotidiens (500 écus)")
async def daily(interaction: discord.Interaction):
    user_id = interaction.user.id
    if can_claim_daily(user_id):
        # L'utilisateur peut récupérer son daily
        balance_before = get_balance(user_id)
        update_balance(user_id, 500)
        claim_daily(user_id)
        balance_after = get_balance(user_id)

        embed = discord.Embed(
            title="🎁 Daily Écus",
            color=discord.Color.green(),
            description="Tu as récupéré tes écus quotidiens !"
        )
        embed.add_field(name="Écus reçus", value="500 écus", inline=True)
        embed.add_field(name="Nouveau solde", value=f"{balance_after} écus", inline=True)
        embed.set_footer(text="Reviens demain pour récupérer tes prochains crédits !")

        await interaction.response.send_message(embed=embed)
        log(f"[DAILY] {interaction.user} a récupéré ses 500 écus quotidiens (Solde: {balance_after})")
    else:
        # L'utilisateur doit attendre
        time_remaining = time_until_next_daily(user_id)
        hours = int(time_remaining // 3600)
        minutes = int((time_remaining % 3600) // 60)

        embed = discord.Embed(
            title="⏰ Daily Écus",
            color=discord.Color.orange(),
            description="Tu as déjà récupéré tes écus quotidiens !"
        )
        embed.add_field(
            name="Temps restant",
            value=f"{hours}h {minutes}m",
            inline=False
        )
        embed.set_footer(text="Patience, tu pourras bientôt récupérer tes prochains crédits !")

        await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------------- ADD CREDITS (ADMIN) ----------------
@tree.command(name="addcredits", description="[ADMIN] Ajouter des écus à un utilisateur")
async def add_credits(interaction: discord.Interaction, user: discord.User, amount: int):
    # Vérifier si l'utilisateur a les permissions d'administrateur
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Seuls les administrateurs peuvent utiliser cette commande.", ephemeral=True)
        return
    
    if amount <= 0:
        await interaction.response.send_message("❌ Le montant doit être positif.", ephemeral=True)
        return
    
    # Obtenir le solde actuel de l'utilisateur cible
    balance_before = get_balance(user.id)
    
    # Ajouter les écus
    update_balance(user.id, amount)
    balance_after = get_balance(user.id)
    
    # Créer un embed de confirmation
    embed = discord.Embed(
    title="💳 Écus ajoutés",
        color=discord.Color.green(),
    description=f"**{amount} écus** ont été ajoutés au compte de {user.mention}"
    )
    embed.add_field(name="Solde avant", value=f"{balance_before} écus", inline=True)
    embed.add_field(name="Montant ajouté", value=f"+{amount} écus", inline=True)
    embed.add_field(name="Nouveau solde", value=f"{balance_after} écus", inline=True)
    embed.set_footer(text=f"Action effectuée par {interaction.user.name}")
    
    await interaction.response.send_message(embed=embed)
    log(f"[ADMIN] {interaction.user} a ajouté {amount} écus à {user} (Nouveau solde: {balance_after})")

# ---------------- RANDOM NUMBER ----------------
@tree.command(name="random", description="Génère un nombre aléatoire entre 1 et le nombre choisi")
async def random_number(interaction: discord.Interaction, maximum: int):
    if maximum < 1:
        await interaction.response.send_message("❌ Le nombre maximum doit être supérieur ou égal à 1.", ephemeral=True)
        return
    
    if maximum > 1000000:
        await interaction.response.send_message("❌ Le nombre maximum ne peut pas dépasser 1 000 000.", ephemeral=True)
        return
    
    result = random.randint(1, maximum)
    
    embed = discord.Embed(
        title="🎲 Nombre aléatoire",
        color=discord.Color.purple(),
        description=f"**Résultat : {result}**"
    )
    embed.add_field(name="Plage", value=f"1 - {maximum}", inline=True)
    embed.set_footer(text=f"Généré pour {interaction.user.display_name}")
    
    await interaction.response.send_message(embed=embed)
    log(f"[RANDOM] {interaction.user} a généré le nombre {result} (1-{maximum})")

# ---------------- HELP ----------------
@tree.command(name="help", description="Affiche toutes les commandes et leur fonctionnement")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎰 Bot Casino - Aide complète",
        color=discord.Color.gold(),
        description="Liste des commandes et mécaniques disponibles :"
    )

    embed.add_field(
        name="💎 Monnaie : écus",
        value=(
            "- Chaque utilisateur commence avec 1000 écus.\n"
            "- Les écus sont sauvegardés dans `data.json` (banque, daily, temps vocal, réglages).\n"
            "- Les mises retirent automatiquement ton solde ; les gains sont ajoutés automatiquement."
        ),
        inline=False
    )

    embed.add_field(
        name="/daily",
        value=(
            "🎁 Récupérer tes écus quotidiens (500 écus).\n"
            "- Utilisable une seule fois toutes les 24 heures.\n"
            "- Le cooldown est précis : attend 24h depuis ta dernière récupération."
        ),
        inline=False
    )

    embed.add_field(
        name="/blackjack <mise>",
        value=(
            "🃏 Blackjack interactif.\n"
            "- Deux cartes te sont distribuées, le croupier en a une cachée.\n"
            "- Utilise les boutons `Hit 🟢` pour tirer et `Stand 🔴` pour rester.\n"
            "- Gains/pénalités appliqués automatiquement à ton solde."
        ),
        inline=False
    )

    embed.add_field(
        name="/roulette",
        value=(
            "🎡 Roulette interactive.\n"
            "- Choisis une case (couleur, pair/impair, douzaine) puis une mise via les menus.\n"
            "- Le message se met à jour à chaque mise pour suivre tes paris.\n"
            "- Règles : numéro exact x35, couleur/pair/impair x2, douzaine x3."
        ),
        inline=False
    )

    embed.add_field(
        name="/slots <mise>",
        value=(
            "🎰 Machine à sous.\n"
            "- Choisis une mise.\n"
            "- Si tu alignes 3 symboles identiques sur la première ligne : x5 ta mise.\n"
            "- Résultat affiché et solde mis à jour automatiquement."
        ),
        inline=False
    )

    embed.add_field(
        name="� /random <maximum>",
        value=(
            "Génère un nombre aléatoire entre 1 et le nombre choisi.\n"
            "- Exemple : `/random 100` génère un nombre entre 1 et 100.\n"
            "- Maximum autorisé : 1 000 000."
        ),
        inline=False
    )

    embed.add_field(
        name="�🏆 /leaderboard",
        value=(
            "Affiche le top 10 des joueurs par solde d'écus.\n"
            "- Utilise les données stockées dans `data.json`."
        ),
        inline=False
    )

    embed.add_field(
        name="⏱️ /voc <utilisateur?>",
        value=(
            "Voir le temps passé en vocal d'un utilisateur (par défaut toi-même).\n"
            "- Le temps est cumulé et tient compte de la session en cours."
        ),
        inline=False
    )

    embed.add_field(
        name="🏆 /vocrank",
        value=(
            "Affiche le top 10 des utilisateurs par temps vocal.\n"
            "- Les sessions en cours sont prises en compte."
        ),
        inline=False
    )

    embed.add_field(
        name="🔧 Règles de rôle vocal (ADMIN)",
        value=(
            "/vocrole_add <role> <min_seconds> <max_seconds> — ajouter une règle qui attribue un rôle si le temps vocal de l'utilisateur est entre min et max (en secondes).\n"
            "/vocrole_remove <role> — supprimer les règles liées à un rôle.\n"
            "/vocrole_list — lister les règles configurées.\n"
            "- Le bot exécute périodiquement (_voc_updater_loop) l'attribution/retrait automatique des rôles selon ces règles."
        ),
        inline=False
    )

    embed.add_field(
        name="🔒 Commandes Admin",
        value=(
            "/addcredits <utilisateur> <montant> — ajouter des écus à un utilisateur (admins seulement).\n"
            "/sync — forcer la synchronisation globale des commandes (admins seulement)."
        ),
        inline=False
    )

    embed.add_field(
        name="🏷️ Grades & temps requis",
        value=(
            "Fou de la gare → 604800s (168h / 7 jours)\n"
            "La frite de devon → 432000s (120h / 5 jours)\n"
            "Jessy nous devont cuit → 259200s (72h / 3 jours)\n"
            "Chèvre de benjamin → 172800s (48h / 2 jours)\n"
            "Toilet de libraité → 86400s (24h / 1 jour)\n"
            "RP Kurt Cobain → 57600s (16h)\n"
            "Creep guy next door → 28800s (8h)\n"
            "LE 10 balles de max → 10800s (3h)\n"
            "Le voisin d'Émile → 3600s (1h)\n"
            "Maluce plus de briquet → 0s (gratuit)\n\n"
        ),
        inline=False
    )

    embed.set_footer(text="Amuse-toi bien au casino ! 🎲 — Données persistées dans data.json")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------- TEMPS VOC ----------------
@tree.command(name="voc", description="Voir le temps passé en vocal d'un utilisateur")
async def voc(interaction: discord.Interaction, user: discord.User = None):
    if user is None:
        user = interaction.user
    user_id = str(user.id)
    total = voc_data.get(user_id, {}).get("total", 0)
    # Si l'utilisateur est actuellement en vocal, ajoute la session en cours
    if user_id in voc_data and voc_data[user_id].get("last_join"):
        total += int(time.time() - voc_data[user_id]["last_join"])
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    embed = discord.Embed(
        title=f"⏱️ Temps vocal de {user.display_name}",
        color=discord.Color.blue(),
        description=f"{hours}h {minutes}m {seconds}s cumulés en vocal."
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="vocrank", description="Affiche le top 10 des utilisateurs par temps vocal")
async def voc_rank(interaction: discord.Interaction):
    # Build leaderboard from voc_data totals (include ongoing sessions)
    entries = []
    now = time.time()
    for uid, d in voc_data.items():
        total = d.get("total", 0)
        if d.get("last_join"):
            total += int(now - d.get("last_join"))
        entries.append((uid, total))
    entries.sort(key=lambda x: x[1], reverse=True)
    description = ""
    for i, (uid, secs) in enumerate(entries[:10], start=1):
        try:
            user = await bot.fetch_user(int(uid))
            name = user.name
        except Exception:
            name = uid
        h = secs // 3600
        m = (secs % 3600) // 60
        s = secs % 60
        description += f"{i}. {name} — {h}h {m}m {s}s\n"
    if not description:
        description = "Aucun enregistrement de temps vocal pour le moment."
    embed = discord.Embed(title="🏆 Classement temps vocal", description=description, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)


@tree.command(name="vocrole_add", description="[ADMIN] Ajouter une règle: si un utilisateur a entre X et Y secondes, lui donner un rôle")
async def vocrole_add(interaction: discord.Interaction, role: discord.Role, min_seconds: int, max_seconds: int):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Seuls les administrateurs peuvent utiliser cette commande.", ephemeral=True)
        return
    if min_seconds < 0 or max_seconds < 0 or max_seconds < min_seconds:
        await interaction.response.send_message("❌ Valeurs invalides pour les secondes.", ephemeral=True)
        return

    # Store rule
    rule = {"min_seconds": int(min_seconds), "max_seconds": int(max_seconds), "role_id": int(role.id)}
    settings.setdefault("voc_role_rules", []).append(rule)
    save_settings()
    await interaction.response.send_message(f"✅ Règle ajoutée: {role.name} pour {min_seconds}s - {max_seconds}s")


@tree.command(name="vocrole_remove", description="[ADMIN] Supprimer une règle par role_id")
async def vocrole_remove(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Seuls les administrateurs peuvent utiliser cette commande.", ephemeral=True)
        return
    before = len(settings.get("voc_role_rules", []))
    settings["voc_role_rules"] = [r for r in settings.get("voc_role_rules", []) if int(r["role_id"]) != int(role.id)]
    after = len(settings.get("voc_role_rules", []))
    save_settings()
    await interaction.response.send_message(f"✅ Règles supprimées pour le rôle {role.name}: {before-after} supprimée(s)")


@tree.command(name="vocrole_list", description="[ADMIN] Lister les règles de rôle voc")
async def vocrole_list(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Seuls les administrateurs peuvent utiliser cette commande.", ephemeral=True)
        return
    lines = []
    for r in settings.get("voc_role_rules", []):
        rid = int(r["role_id"])
        # Try to find role name in current guilds
        role_name = None
        for g in bot.guilds:
            role = g.get_role(rid)
            if role:
                role_name = f"{role.name} (guild: {g.name})"
                break
        if not role_name:
            role_name = str(rid)
        lines.append(f"- {role_name}: {r['min_seconds']}s - {r['max_seconds']}s")
    if not lines:
        await interaction.response.send_message("Aucune règle configurée.", ephemeral=True)
    else:
        embed = discord.Embed(title="Règles voc role", description="\n".join(lines), color=discord.Color.blue())
        await interaction.response.send_message(embed=embed, ephemeral=True)



# ---------------- ON READY ----------------
@bot.event
async def on_ready():
    print(f"✅ Connecté en tant que {bot.user}")
    print(f"🌐 Serveurs connectés: {len(bot.guilds)}")
    for guild in bot.guilds:
        print(f"   - {guild.name} (ID: {guild.id})")
    
    # Synchronisation des commandes
    if GUILD_IDS and GUILD_IDS[0]:  # Si des guild IDs sont définis
        print("🔄 Synchronisation des commandes par serveur (instantané)...")
        for guild_id in GUILD_IDS:
            try:
                guild_id = int(guild_id.strip())
                guild_obj = discord.Object(id=guild_id)
                
                # Trouver le nom du serveur
                guild_name = "Serveur inconnu"
                for guild in bot.guilds:
                    if guild.id == guild_id:
                        guild_name = guild.name
                        break
                
                await tree.sync(guild=guild_obj)
                print(f"✅ Commandes synchronisées pour '{guild_name}' (ID: {guild_id})")
            except Exception as e:
                print(f"❌ Erreur lors de la synchronisation pour le serveur {guild_id}: {e}")
    else:
        # Synchronisation globale (propagation lente, ~1h)
        await tree.sync()
        print("✅ Commandes synchronisées globalement (propagation lente)")
    
    print("📌 Commandes disponibles :", [cmd.name for cmd in tree.get_commands()])
    
    # Vérifier les permissions dans chaque serveur
    for guild in bot.guilds:
        me = guild.me
        print(f"🔐 Permissions dans '{guild.name}':")
        print(f"   - send_messages: {me.guild_permissions.send_messages}")
        print(f"   - embed_links: {me.guild_permissions.embed_links}")
        print(f"   - use_application_commands: {me.guild_permissions.use_application_commands}")
        if not me.guild_permissions.use_application_commands:
            print(f"   ⚠️  ATTENTION: Le bot n'a pas la permission use_application_commands !")
    
    print("🚀 Bot prêt à recevoir des commandes !")

    # Initialize last_join for members already in voice channels (after restart)
    changed = False
    now = time.time()
    for guild in bot.guilds:
        for vc in getattr(guild, 'voice_channels', []):
            for member in vc.members:
                uid = str(member.id)
                voc_data.setdefault(uid, {"total": 0, "last_join": None})
                if not voc_data[uid].get("last_join"):
                    voc_data[uid]["last_join"] = now
                    changed = True
    if changed:
        save_voc()

    # start background updater
    bot.loop.create_task(_voc_updater_loop())


@tree.command(name="sync", description="[ADMIN] Forcer la synchronisation des commandes")
async def sync_commands(interaction: discord.Interaction):
    # Restreint aux administrateurs
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Seuls les administrateurs peuvent utiliser cette commande.", ephemeral=True)
        return

    # Sync pour le serveur actuel ou global
    if interaction.guild_id:
        guild_name = interaction.guild.name if interaction.guild else "Serveur inconnu"
        await tree.sync(guild=interaction.guild)
        await interaction.response.send_message(f"✅ Synchronisation effectuée pour **{guild_name}** (instantané)")
        print(f"🔄 [SYNC] Synchronisation manuelle effectuée pour '{guild_name}' (ID: {interaction.guild_id}) par {interaction.user}")
    else:
        await tree.sync()
        await interaction.response.send_message("✅ Synchronisation globale lancée (propagation lente)")
        print(f"🔄 [SYNC] Synchronisation globale manuelle effectuée par {interaction.user}")

bot.run(TOKEN)
